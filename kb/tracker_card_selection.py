"""Grounded presentation of one value from a previously retrieved query result."""
from .publication import digest
from .tracker_query import encode
from .value_cards import PRESENTATION_NOTICE, selected_value_card


def selection_result(result, source_digest, selection):
    chart = selected_value_card(result, **selection)
    return {'kind': 'tracker_value', 'source_result_id': source_digest,
            'selection': {key: selection[key] for key in ('field', 'row_id', 'group')},
            'chart': chart, 'presentation': 'value_card', 'presentation_notice': PRESENTATION_NOTICE}


def replay_source(service, query, cap, source_digest):
    if (not isinstance(query, dict) or not query.get('kb_slug') or query.get('status', 'done') != 'done'
            or any(query.get(key) for key in ('_export', '_matching_ids'))):
        raise ValueError('Choose a lookup of approved source records.')
    result = service.lookup(cap=cap, **query)
    if digest(encode(result)) != source_digest:
        raise ValueError('Source data or access changed. Retrieve the relevant data again before presenting it.')
    return result


def validate_selection(snapshot, allowed):
    from .tracker_query import TrackerQuery, RESPONSE_CHARS
    try:
        query = snapshot['query']
        cap = snapshot['response_chars']
        request = snapshot['presentation_query']
        if (not isinstance(cap, int) or isinstance(cap, bool) or not 1200 <= cap <= RESPONSE_CHARS
                or not isinstance(request, dict)
                or set(request) != {'source_result_digest', 'field', 'row_id', 'group', 'title', 'unit'}
                or not isinstance(snapshot.get('text'), str)
                or digest(snapshot['text']) != snapshot.get('result_digest')):
            return False
        selection = {key: value for key, value in request.items() if key != 'source_result_digest'}
        source = replay_source(TrackerQuery(allowed), query, cap, request['source_result_digest'])
        expected = selection_result(source, request['source_result_digest'], selection)
        return snapshot['text'] == encode(expected) and snapshot.get('chart') == expected['chart']
    except (KeyError, ValueError, TypeError, OverflowError):
        return False
