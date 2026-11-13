"""Grounded image directories for existing text-only retrieval chunks."""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from kb.chunk_images import image_metadata_current, linked_images
from kb.pipeline import _md_for_embedding
from kb.publication import digest


class ChunkImageTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings_override = override_settings(DATA_DIR=Path(self.temp.name))
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.doc = SimpleNamespace(pk=uuid4(), original_name="Business manual.pdf",
            kb=SimpleNamespace(slug="operations"), md_content="")
        self.image_dir = Path(self.temp.name) / "doc_images" / str(self.doc.pk)
        self.image_dir.mkdir(parents=True)
        self.names = [f"{i:032x}.png" for i in range(1, 13)]
        for name in self.names:
            (self.image_dir / name).write_bytes(b"verified-local-image")

    def row(self, text, **extra):
        return dict(text=text, chunk_id="text-chunk-1", **extra)

    def metadata(self, blocks):
        directory = Path(self.temp.name) / "doc_meta" / str(self.doc.pk)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "content_list.json").write_text(json.dumps(blocks), encoding="utf-8")

    def image_block(self, name=None, **extra):
        block = dict(type="image", img_path=f"images/{name or self.names[0]}")
        block.update(extra)
        return block

    def test_direct_markdown_has_grounded_caption_identity_and_original_position(self):
        text = f"Current measurements. ![Motor inspection](images/{self.names[0]})"
        result = linked_images(self.doc, self.row(text))
        self.assertEqual(len(result), 1)
        image = result[0]
        self.assertEqual(image["text"], "Motor inspection")
        self.assertEqual(image["type"], "image")
        self.assertEqual(image["image"], self.names[0])
        self.assertEqual(image["doc_id"], str(self.doc.pk))
        self.assertEqual(image["source"], self.doc.original_name)
        self.assertEqual(image["kb_slug"], self.doc.kb.slug)
        self.assertEqual(image["chunk_id"], f"img-{self.doc.pk}-{self.names[0]}")
        self.assertEqual(image["relation"], "embedded_reference")
        self.assertEqual(image["parent_chunk_id"], "text-chunk-1")
        self.assertEqual(image["parent_chunk_digest"], digest(text))
        position = image["position"]
        self.assertEqual(position["basis"], "chunk_text")
        self.assertEqual(text[position["start"]:position["end"]], text[text.index("!["):])

    def test_html_protected_url_unquoted_src_and_entity_caption_are_supported(self):
        text = f'<IMG src=/kb/doc/{self.doc.pk}/img/{self.names[0]} alt="Load &amp; power">'
        result = linked_images(self.doc, self.row(text))
        self.assertEqual([item["text"] for item in result], ["Load & power"])

    def test_markdown_angle_target_and_optional_title_are_supported(self):
        for target in (f"<images/{self.names[0]}>", f'images/{self.names[0]} "Figure one"'):
            with self.subTest(target=target):
                self.assertEqual(len(linked_images(self.doc, self.row(f"![Figure]({target})"))), 1)

    def test_external_foreign_encoded_and_traversal_targets_are_rejected(self):
        for target in (f"https://example.org/images/{self.names[0]}",
                f"//example.org/images/{self.names[0]}",
                f"/kb/doc/{uuid4()}/img/{self.names[0]}",
                f"images/../{self.names[0]}", f"images/%2e%2e/{self.names[0]}",
                f"images/{self.names[0]}?token=1", f"images/{self.names[0]}#caption",
                f"images\\{self.names[0]}", f"file:///images/{self.names[0]}",
                "images/not-a-content-hash.png"):
            with self.subTest(target=target):
                self.assertEqual(linked_images(self.doc, self.row(f"![x]({target})")), [])

    def test_duplicate_src_html_is_rejected(self):
        text = f'<img src="images/{self.names[0]}" src="https://outside.invalid/p.png">'
        self.assertEqual(linked_images(self.doc, self.row(text)), [])

    def test_missing_files_are_not_candidates(self):
        (self.image_dir / self.names[0]).unlink()
        self.assertEqual(linked_images(self.doc, self.row(f"![Missing](images/{self.names[0]})")), [])

    def test_stripped_markdown_refs_are_recovered_at_the_original_position(self):
        self.doc.md_content = ("## Motor readings\n\nThe recorded torque is above the operating baseline.\n\n"
            f"![Torque plot](images/{self.names[0]})\n\n"
            "The following inspection found no structural deterioration.")
        text = _md_for_embedding(self.doc.md_content)
        result = linked_images(self.doc, self.row(text))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["relation"], "embedded_reference")
        self.assertEqual(result[0]["text"], "Torque plot")
        self.assertEqual(result[0]["position"]["basis"], "document_markdown")
        pos = result[0]["position"]
        self.assertTrue(self.doc.md_content[pos["start"]:pos["end"]].startswith("![Torque plot]"))

    def test_cleaner_handles_tables_entities_and_synthetic_section_prefix(self):
        self.doc.md_content = ("## Annual output\n\nThe production line completed its planned annual inspection.\n\n"
            "<table><tr><td>Budget &amp; cost</td><td>12</td></tr></table>\n\n"
            f'<img src="images/{self.names[0]}" alt="Annual comparison">\n\n'
            "The annual operating budget remains within the approved threshold.")
        text = "【Annual output】\n" + _md_for_embedding(self.doc.md_content)
        result = linked_images(self.doc, self.row(text))
        self.assertEqual([item["text"] for item in result], ["Annual comparison"])

    def test_images_at_a_cleaned_chunk_boundary_are_labelled_adjacent(self):
        text = "Inspect the equipment before commencing the maintenance procedure."
        for original in (f"![Before](images/{self.names[0]})\n\n{text}",
                f"{text}\n\n![After](images/{self.names[0]})"):
            with self.subTest(original=original):
                self.doc.md_content = original
                result = linked_images(self.doc, self.row(text))
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]["relation"], "adjacent_reference")

    def test_repeated_text_does_not_borrow_another_occurrences_figures(self):
        text = "The standard operating procedure applies to all equipment."
        self.doc.md_content = f"{text}\n\n![First](images/{self.names[0]})\n\n{text}"
        self.assertEqual(linked_images(self.doc, self.row(text)), [])

    def test_unread_sections_and_bare_document_images_are_not_exposed(self):
        text = "The current inspection confirmed acceptable equipment operation."
        self.doc.md_content = (f"{text}\n\n## Another section\n\n"
            f"Details of an unrelated equipment replacement.\n\n![Secret](images/{self.names[0]})")
        self.metadata([self.image_block(page_idx=0, image_caption=["Unrelated caption"])])
        self.assertEqual(linked_images(self.doc, self.row(text, page_start=0, page_end=0)), [])

    def test_content_list_only_image_requires_read_text_on_both_sides(self):
        before = "Equipment A operated continuously throughout the measurement period."
        after = "The subsequent analysis confirmed a stable daily operating trend."
        self.doc.md_content = f"{before}\n\n{after}"
        self.metadata([{"type": "text", "text": before, "page_idx": 2},
            self.image_block(page_idx=2, image_caption=["Trend during inspection"]),
            {"type": "text", "text": after, "page_idx": 2}])
        result = linked_images(self.doc, self.row(f"{before}\n\n{after}"))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["relation"], "content_list_sequence")
        self.assertEqual(result[0]["text"], "Trend during inspection")
        self.assertEqual(result[0]["page_label"], "第 3 页")
        self.assertEqual(result[0]["position"], {"basis": "content_list", "block_index": 1})
        self.assertEqual(linked_images(self.doc, self.row(before)), [])
        self.assertEqual(linked_images(self.doc, self.row(after)), [])

    def test_foreign_content_list_paths_do_not_bypass_reference_checks(self):
        before = "Equipment A operated continuously throughout the measurement period."
        after = "The subsequent analysis confirmed a stable daily operating trend."
        self.metadata([{"type": "text", "text": before},
            self.image_block(img_path=f"https://example.org/images/{self.names[0]}"),
            {"type": "text", "text": after}])
        self.assertEqual(linked_images(self.doc, self.row(before + after)), [])

    def test_metadata_caption_and_page_are_used_without_unread_text_context(self):
        self.metadata([self.image_block(page_idx=4, image_caption=["Measured load"]),
            {"type": "text", "text": "UNREAD SECRET FROM ANOTHER CHUNK", "page_idx": 4}])
        result = linked_images(self.doc, self.row(f"![](images/{self.names[0]})"))
        self.assertEqual(result[0]["text"], "Measured load")
        self.assertEqual(result[0]["page_label"], "第 5 页")
        self.assertNotIn("UNREAD SECRET", json.dumps(result))

    def test_reused_image_on_multiple_pages_has_no_misleading_single_page_label(self):
        self.metadata([self.image_block(page_idx=1), self.image_block(page_idx=7)])
        result = linked_images(self.doc, self.row(f"![Diagram](images/{self.names[0]})"))
        self.assertNotIn("page_label", result[0])

    def test_duplicates_and_limits_bound_the_directory(self):
        text = " ".join(f"![Image {i}](images/{name})" for i, name in enumerate(self.names))
        text += f" ![Duplicate](images/{self.names[0]})"
        self.assertEqual(len(linked_images(self.doc, self.row(text))), 8)
        self.assertEqual(len(linked_images(self.doc, self.row(text), limit=99)), 8)
        self.assertEqual(len(linked_images(self.doc, self.row(text), limit=2)), 2)
        self.assertEqual(linked_images(self.doc, self.row(text), limit=0), [])
        self.assertEqual(linked_images(self.doc, self.row(text), limit="bad"), [])

    def test_image_rows_and_empty_chunks_do_not_expand_recursively(self):
        text = f"![Figure](images/{self.names[0]})"
        self.assertEqual(linked_images(self.doc, self.row(text, type="image")), [])
        self.assertEqual(linked_images(self.doc, self.row("")), [])

    def test_foreign_parent_identity_is_rejected(self):
        text = f"![Figure](images/{self.names[0]})"
        for wrong in ({"doc_id": str(uuid4())}, {"kb_slug": "another-kb"}, {"source": "Other.pdf"}):
            with self.subTest(wrong=wrong):
                self.assertEqual(linked_images(self.doc, self.row(text, **wrong)), [])

    def test_empty_image_caption_uses_only_clean_nearby_read_text(self):
        text = f"The tested motor operates within limits. ![](images/{self.names[0]})"
        result = linked_images(self.doc, self.row(text))
        self.assertEqual(result[0]["text"], "The tested motor operates within limits.")

    def test_malformed_content_list_blocks_do_not_break_direct_image_discovery(self):
        self.metadata([{"type": "text", "text": {"unexpected": "object"}},
            {"type": "table", "table_caption": [1, 2]}, self.image_block(page_idx=True)])
        result = linked_images(self.doc, self.row(f"![Figure](images/{self.names[0]})"))
        self.assertEqual(len(result), 1)
        self.assertNotIn("page_label", result[0])

    def test_direct_reference_detects_metadata_caption_and_page_changes(self):
        original = self.image_block(page_idx=2, image_caption=["Measured annual output"])
        self.metadata([original])
        image = linked_images(self.doc, self.row(f"![](images/{self.names[0]})"))[0]
        self.assertTrue(image_metadata_current(self.doc, image))
        self.assertEqual(image["text"], "Measured annual output")
        for changes in ({"image_caption": ["Corrected annual output"]}, {"page_idx": 3}):
            with self.subTest(changes=changes):
                self.metadata([dict(original, **changes)])
                self.assertFalse(image_metadata_current(self.doc, image))
        self.metadata([original])
        self.assertTrue(image_metadata_current(self.doc, image))

    def test_metadata_addition_or_removal_invalidates_only_that_image_stamp(self):
        image = linked_images(self.doc, self.row(f"![Diagram](images/{self.names[0]})"))[0]
        self.assertTrue(image_metadata_current(self.doc, image))
        self.metadata([self.image_block(page_idx=2, image_caption=["New caption"])])
        self.assertFalse(image_metadata_current(self.doc, image))
        refreshed = linked_images(self.doc, self.row(f"![Diagram](images/{self.names[0]})"))[0]
        self.assertTrue(image_metadata_current(self.doc, refreshed))
        self.metadata([])
        self.assertFalse(image_metadata_current(self.doc, refreshed))
        self.assertTrue(image_metadata_current(self.doc, image))

    def test_unrelated_metadata_and_unused_layout_fields_do_not_invalidate(self):
        original = self.image_block(page_idx=2, image_caption=["Measured output"], bbox=[0, 0, 10, 10])
        self.metadata([original, self.image_block(self.names[1], page_idx=3)])
        image = linked_images(self.doc, self.row(f"![](images/{self.names[0]})"))[0]
        self.metadata([{"type": "text", "text": "Changed unrelated prose"},
            self.image_block(self.names[1], page_idx=8, image_caption=["Changed another image"]),
            dict(original, bbox=[20, 20, 40, 40])])
        self.assertTrue(image_metadata_current(self.doc, image))

    def test_metadata_validator_accepts_snapshot_shape_and_rejects_foreign_or_unstamped(self):
        self.metadata([self.image_block(page_idx=2, image_caption=["Measured output"])])
        image = linked_images(self.doc, self.row(f"![](images/{self.names[0]})"))[0]
        snapshot = {key: image[key] for key in ("image", "image_metadata_digest", "doc_id", "kb_slug")}
        self.assertTrue(image_metadata_current(self.doc, snapshot))
        self.assertFalse(image_metadata_current(self.doc, dict(snapshot, doc_id=str(uuid4()))))
        self.assertFalse(image_metadata_current(self.doc, dict(snapshot, kb_slug="foreign-kb")))
        snapshot.pop("image_metadata_digest")
        self.assertFalse(image_metadata_current(self.doc, snapshot))

