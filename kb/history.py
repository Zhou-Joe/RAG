"""Bound chat memory to complete user/answer pairs, without old tool payloads."""
def recent_history(messages, *, enhanced, visible, max_chars=8000, max_pairs=3):
    pairs = []
    question = None
    for item in messages:
        if item.role == 'user':
            question = item.content
            continue
        if item.role != 'ai' or question is None:
            continue
        if (enhanced and (not item.verified or not item.citations)) or (item.citations and not visible(item.citations)):
            question = None
            pairs = []
            continue
        pairs.append((question, item.content))
        question = None
    chosen = []
    size = 0
    for question, answer in reversed(pairs[-max_pairs:]):
        length = len(question) + len(answer)
        if size + length > max_chars:
            break  # Do not truncate facts halfway through a sentence/table row.
        chosen.insert(0, [{'role':'user','content':question}, {'role':'assistant','content':answer}])
        size += length
    return [message for pair in chosen for message in pair]
