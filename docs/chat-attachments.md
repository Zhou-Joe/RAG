# EnGenie Chat Attachments

Use the paperclip in the chat composer to select photos or files. Drag and drop, pasted screenshots, removable previews, and attachment-only messages are supported. Failed uploads retain the selected files for retry; successful uploads appear in the conversation history.

Supported formats: JPEG, PNG, WebP, PDF, DOCX, TXT, Markdown, XLSX, XLSM, CSV and TSV. Files belong to the conversation and are not automatically added to a knowledge base or embedded.

Images are decoded, oriented and resized for actual multimodal model input. A vision-enabled model is required for photos and visual PDFs. With vision enabled, PDFs of up to eight pages include both extracted text and page images. Larger PDFs containing images are rejected rather than silently omitting their visual content. DOCX is read as text; documents containing embedded images carry an explicit notice to upload those images separately.

Uploads are limited to five files, 10 MiB per file and 25 MiB per message. A message may contain up to eight images or rendered PDF pages. Extracted text is limited to 80,000 characters per document and 120,000 characters across retained conversation attachments. The retained context supports up to 20 files and 16 images/PDF pages; exceeding these bounds asks for a new conversation without silently discarding earlier source material.

Original files are stored privately under `data/chat_attachments` by default. `CHAT_ATTACHMENT_ROOT` may select a different directory outside public media and static directories. Files have no public media URL; authenticated download checks both conversation ownership and current access. Include this private directory in application backups. Deleting a conversation removes its attachment records and schedules physical-file cleanup after the database commit.

Completed model transcripts store attachment references rather than base64 image payloads. Follow-up turns revalidate and reload the referenced files. Enhanced answer verification receives the actual uploaded text and image pixels and checks their stored fingerprints.

Deployment requires migration `0059_chat_attachments` and the preceding migrations, static collection, and a web-process restart. The existing vision-enabled model setting remains in effect.
