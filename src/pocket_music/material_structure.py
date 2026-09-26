# SPDX-License-Identifier: AGPL-3.0-only
"""Immutable, attributed phrase graphs over exact material revisions.

This provider records boundaries and relationships; it does not infer motif
identity, arrange notes, schedule playback, or establish human listening.
"""
from __future__ import annotations

import copy
import json
from collections import deque
from pathlib import Path
from typing import Literal

from .artifact_store import (
    ArtifactHandle,
    canonical_bytes,
    digest,
    put_record,
    read_bytes,
    read_record,
    receipt,
    run_request,
)
from .errors import PocketError
from .material import integer, load_material, rational, resolve_selection
from .structure_types import StructureDefinition

SCHEMA = 'pocket.material-structure/v1'
COVERAGE = {
    'material_bindings': 'exact_revision_verified', 'span_membership': 'explicit_note_onsets_half_open',
    'phrase_boundaries': 'attributed', 'motif_identity': 'attributed_not_inferred',
    'derivation_links': 'declared_material_lineage_verified', 'expression': 'retained_in_material_not_interpreted',
    'cycles': 'rejected', 'native_execution': False, 'human_listening': 'not_established',
}
DEFINITION_FIELDS = {'label', 'materials', 'nodes', 'relations', 'attribution', 'cycle_policy'}
NODE_FIELDS = {'node_id', 'kind', 'label', 'material_key', 'material_revision', 'clip_id', 'space', 'span_qn', 'note_ids', 'attribution'}
NODE_DERIVED = {'material', 'selection_sha256', 'note_count', 'source_clip_origin', 'gate_crossing_count'}
RELATION_FIELDS = {'relation_id', 'from_node', 'to_node', 'kind', 'identity_claims', 'attribution'}
RELATIONS = {'sequence', 'repeat', 'variation', 'withhold', 'return', 'call_response', 'motif_reference', 'derivation'}
IDENTITIES = {'exact_events', 'rhythm', 'pitch_intervals', 'accents', 'perceptual'}


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise PocketError('Missing or unexpected material-structure fields')


def _text(value, name, maximum=120):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise PocketError(f'{name} requires bounded nonempty text')
    return value


def _array(value, name, minimum=0, maximum=4096):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise PocketError(f'{name} requires {minimum}–{maximum} entries')
    return value


def _rational(value):
    _fields(value, {'n', 'd'})
    integer(value['n'], 'quarter-note numerator', -(2**53), 2**53)
    integer(value['d'], 'quarter-note denominator', 1, 2**53)
    return rational(value)


def _handle(value):
    _fields(value, {'schema', 'artifact_uri', 'sha256', 'artifact_schema'})
    if value['schema'] != 'pocket.artifact-handle/v1':
        raise PocketError('Expected a versioned structure dependency handle')
    return value


def _references(value, store_root, context, depth=0):
    if depth > 64:
        raise PocketError('Structure evidence reference nesting exceeds 64')
    if isinstance(value, dict) and value.get('schema') == 'pocket.artifact-handle/v1':
        _handle(value)
        key = canonical_bytes(value)
        if key in context['references']:
            return
        if len(context['references']) >= 4096:
            raise PocketError('Structure evidence exceeds 4096 distinct artifact handles')
        context['references'].add(key)
        raw = read_bytes(value, store_root)
        if Path(value['artifact_uri']).name == 'record.json':
            context['metadata_bytes'] += len(raw)
            if context['metadata_bytes'] > 64 * 1024 * 1024:
                raise PocketError('Structure evidence metadata exceeds 64 MiB')
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeError) as error:
                raise PocketError('Invalid JSON structure evidence') from error
            if not isinstance(record, dict) or record.get('schema') != value['artifact_schema']:
                raise PocketError('Structure evidence artifact schema mismatch')
            canonical_bytes(record)
            _references(record, store_root, context, depth + 1)
    elif isinstance(value, dict):
        for child in value.values():
            _references(child, store_root, context, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _references(child, store_root, context, depth + 1)


def _attribution(value, store_root, context):
    _fields(value, {'actor', 'actor_kind', 'statement', 'uncertainty', 'evidence'})
    _text(value['actor'], 'actor', 200)
    _text(value['statement'], 'statement', 2000)
    if value['actor_kind'] not in ('human', 'agent'):
        raise PocketError('Attribution requires an explicit human or agent actor')
    for uncertainty in _array(value['uncertainty'], 'uncertainty', maximum=8):
        _text(uncertainty, 'uncertainty', 500)
    for evidence in _array(value['evidence'], 'evidence', maximum=8):
        _references(_handle(evidence), store_root, context)
    return copy.deepcopy(value)


def _context():
    return {'references': set(), 'materials': {}, 'material_notes': 0, 'metadata_bytes': 0}


def _material(value, store_root, context):
    # Both literal and handle input normalize to canonical record.json identity.
    if isinstance(value, dict) and value.get('schema') == 'pocket.artifact-handle/v1':
        _handle(value)
    record = load_material(value, store_root)
    sha = digest(record)
    if sha not in context['materials']:
        context['material_notes'] += len(record['notes'])
        if context['material_notes'] > 200000:
            raise PocketError('Structure material inspection exceeds 200000 distinct notes')
        _references(record, store_root, context)
        context['materials'][sha] = record
    handle = {'schema': 'pocket.artifact-handle/v1', 'artifact_uri': f'artifacts/{sha}/record.json',
              'sha256': sha, 'artifact_schema': record['schema']}
    return handle, context['materials'][sha]


def _acyclic(nodes, relations):
    edges = {node: [] for node in nodes}
    indegree = dict.fromkeys(nodes, 0)
    for relation in relations:
        edges[relation['from_node']].append(relation['to_node'])
        indegree[relation['to_node']] += 1
    pending = deque(node for node, degree in indegree.items() if degree == 0)
    visited = 0
    while pending:
        current = pending.popleft()
        visited += 1
        for target in edges[current]:
            indegree[target] -= 1
            if not indegree[target]:
                pending.append(target)
    if visited == len(nodes):
        return
    colors = {}
    for start in nodes:
        if colors.get(start):
            continue
        stack, path, positions = [(start, iter(edges[start]))], [start], {start: 0}
        colors[start] = 1
        while stack:
            current, children = stack[-1]
            target = next(children, None)
            if target is None:
                colors[current] = 2
                stack.pop()
                positions.pop(current)
                path.pop()
            elif colors.get(target) == 1:
                cycle = path[positions[target]:] + [target]
                shown = cycle[:8]
                suffix = f' ({len(cycle)} nodes in cycle path)' if len(cycle) > 8 else ''
                raise PocketError('Directed structure cycle rejected: ' + ' -> '.join(shown) + suffix)
            elif not colors.get(target):
                colors[target] = 1
                positions[target] = len(path)
                path.append(target)
                stack.append((target, iter(edges[target])))
    raise PocketError('Directed structure cycle rejected')


def _normalize(definition, store_root, context, ancestry):
    _fields(definition, DEFINITION_FIELDS, {'parent_structure'})
    canonical_bytes(definition)
    _text(definition['label'], 'structure label', 200)
    if definition['cycle_policy'] != 'reject':
        raise PocketError('Initial structure cycle_policy must be reject; returns use new occurrences')
    attribution = _attribution(definition['attribution'], store_root, context)
    parent = definition.get('parent_structure')
    if parent is not None:
        _load(parent, store_root, context, ancestry)
    materials, bindings = [], {}
    for binding in _array(definition['materials'], 'material bindings', 1, 64):
        _fields(binding, {'key', 'material'})
        key = _text(binding['key'], 'material key')
        if key in bindings:
            raise PocketError('Duplicate material binding key')
        handle, record = _material(binding['material'], store_root, context)
        bindings[key] = (handle, record)
        materials.append({'key': key, 'material': handle, 'material_id': record['material_id'],
                          'material_revision': record['revision_sha256']})
    nodes, by_id, memberships = [], {}, 0
    for node in _array(definition['nodes'], 'phrase nodes', 1, 1024):
        _fields(node, NODE_FIELDS)
        node_id = _text(node['node_id'], 'node ID')
        if node_id in by_id:
            raise PocketError('Duplicate phrase node ID')
        if node['kind'] not in ('phrase', 'motif_reference') or node['space'] != 'clip_qn':
            raise PocketError('Phrase nodes require supported kind and explicit clip_qn space')
        _text(node['label'], 'node label', 200)
        _text(node['material_key'], 'material key')
        if node['material_key'] not in bindings:
            raise PocketError('Unknown phrase material key')
        handle, material = bindings[node['material_key']]
        if node['material_revision'] != material['revision_sha256']:
            raise PocketError('Stale phrase material revision')
        _text(node['clip_id'], 'clip ID')
        matches = [clip for clip in material['clips'] if clip['id'] == node['clip_id']]
        if len(matches) != 1:
            raise PocketError('Unknown phrase clip occurrence')
        clip = matches[0]
        _fields(node['span_qn'], {'start', 'end'})
        start, end = _rational(node['span_qn']['start']), _rational(node['span_qn']['end'])
        if start >= end or end > rational(clip['length_qn']):
            raise PocketError('Phrase span must increase and end within its bound clip')
        note_ids = _array(node['note_ids'], 'phrase note IDs', maximum=4096)
        for note_id in note_ids:
            _text(note_id, 'note ID')
        if len(note_ids) != len(set(note_ids)):
            raise PocketError('Duplicate phrase member ID')
        memberships += len(note_ids)
        if memberships > 16384:
            raise PocketError('Structure exceeds 16384 declared memberships')
        selection = resolve_selection(material, {'clip_ids': [clip['id']], 'note_ids': note_ids,
                                                'span_qn': [node['span_qn']['start'], node['span_qn']['end']]})
        if set(selection['note_ids']) != set(note_ids):
            raise PocketError('Phrase member belongs to another clip or is outside its onset span')
        note_index = {note['id']: note for note in material['notes']}
        crossing = sum(rational(note_index[key]['onset']) + rational(note_index[key]['duration_qn']) > end
                       for key in note_ids)
        normalized = {**copy.deepcopy(node), 'attribution': _attribution(node['attribution'], store_root, context),
                      'material': handle, 'selection_sha256': selection['selection_sha256'],
                      'note_count': len(note_ids), 'source_clip_origin': copy.deepcopy(clip['origin']),
                      'gate_crossing_count': crossing}
        nodes.append(normalized)
        by_id[node_id] = normalized
    relations, relation_ids, links = [], set(), set()
    for relation in _array(definition['relations'], 'phrase relations', maximum=4096):
        _fields(relation, RELATION_FIELDS)
        relation_id = _text(relation['relation_id'], 'relation ID')
        if relation_id in relation_ids:
            raise PocketError('Duplicate phrase relation ID')
        relation_ids.add(relation_id)
        for field in ('from_node', 'to_node'):
            _text(relation[field], 'relation endpoint')
            if relation[field] not in by_id:
                raise PocketError('Unresolved phrase relation endpoint')
        kind = _text(relation['kind'], 'relation kind')
        if kind not in RELATIONS:
            raise PocketError('Unsupported phrase relation kind')
        edge = (relation['from_node'], relation['to_node'], kind)
        if edge[0] == edge[1] or edge in links:
            raise PocketError('Self-edge or duplicate phrase relationship')
        links.add(edge)
        claims = _array(relation['identity_claims'], 'identity claims', maximum=5)
        if any(not isinstance(claim, str) or claim not in IDENTITIES for claim in claims) or len(set(claims)) != len(claims):
            raise PocketError('Invalid or duplicate attributed motif identity claims')
        if kind == 'derivation':
            source = bindings[by_id[edge[0]]['material_key']][1]
            child = bindings[by_id[edge[1]]['material_key']][1]
            if child['material_id'] != source['material_id'] or child['parent_revision'] != source['revision_sha256']:
                raise PocketError('Derivation does not match declared material parent lineage')
        relations.append({**copy.deepcopy(relation),
                          'attribution': _attribution(relation['attribution'], store_root, context)})
    _acyclic(by_id, relations)
    return {'schema': SCHEMA, 'label': definition['label'], 'parent_structure': copy.deepcopy(parent),
            'cycle_policy': 'reject', 'attribution': attribution, 'materials': materials,
            'nodes': nodes, 'relations': relations, 'coverage': copy.deepcopy(COVERAGE)}


def _load(handle, store_root, context=None, ancestry=()):
    context = _context() if context is None else context
    _handle(handle)
    if len(ancestry) >= 32:
        raise PocketError('Structure parent chain exceeds 32 artifacts')
    if handle['sha256'] in ancestry:
        raise PocketError('Cyclic structure parent chain')
    record = read_record(handle, store_root, SCHEMA)
    _fields(record, DEFINITION_FIELDS | {'schema', 'parent_structure', 'coverage'})
    bindings = []
    for binding in _array(record['materials'], 'stored material bindings', 1, 64):
        _fields(binding, {'key', 'material', 'material_id', 'material_revision'})
        bindings.append({'key': binding['key'], 'material': binding['material']})
    nodes = []
    for node in _array(record['nodes'], 'stored phrase nodes', 1, 1024):
        _fields(node, NODE_FIELDS | NODE_DERIVED)
        nodes.append({key: node[key] for key in NODE_FIELDS})
    definition = {key: record[key] for key in DEFINITION_FIELDS | {'parent_structure'}}
    definition.update(materials=bindings, nodes=nodes)
    normalized = _normalize(definition, store_root, context, (*ancestry, handle['sha256']))
    if canonical_bytes(normalized) != canonical_bytes(record):
        raise PocketError('Stored structure derived identities or coverage do not match its sources')
    return record


def _compact_node(node):
    return {**{key: copy.deepcopy(value) for key, value in node.items() if key != 'note_ids'},
            'note_ids_omitted': node['note_count']}


def material_structure(operation: Literal['create', 'query'], store_root: str,
                       request_id: str | None = None, definition: StructureDefinition | None = None,
                       structure: ArtifactHandle | None = None,
                       section: Literal['summary', 'nodes', 'relations', 'members'] = 'summary',
                       node_ids: list[str] | None = None, limit: int = 64, cursor: str | None = None,
                       max_bytes: int = 16000) -> dict:
    """Create or query an exact material-bound phrase graph with supplied interpretations."""
    integer(limit, 'structure query limit', 1, 256)
    integer(max_bytes, 'structure query byte budget', 1024, 65536)
    if operation == 'create':
        if (definition is None or structure is not None or section != 'summary' or node_ids is not None
                or limit != 64 or cursor is not None or max_bytes != 16000):
            raise PocketError('Structure create requires only definition, store_root and request_id')
        def work():
            context = _context()
            checked = _normalize(definition, store_root, context, ('new-structure',))
            # Publish only after every supplied binding, relation and parent validates.
            for material in context['materials'].values():
                put_record(material, store_root)
            handle = put_record(checked, store_root)
            return receipt(request_id, artifacts={'structure': handle}, coverage=copy.deepcopy(COVERAGE),
                           change_summary={'materials': len(checked['materials']), 'nodes': len(checked['nodes']),
                                           'relations': len(checked['relations']),
                                           'memberships': sum(n['note_count'] for n in checked['nodes'])},
                           uncertainty=['Phrase boundaries and motif identity remain attributed interpretations.'])
        result = run_request(store_root, request_id, 'material_structure.create', {'definition': definition}, work)
        _load(result['artifacts']['structure'], store_root)
        return result
    if operation != 'query' or definition is not None or structure is None or request_id is not None:
        raise PocketError('Structure query requires structure and omits definition/request_id')
    if section not in ('summary', 'nodes', 'relations', 'members'):
        raise PocketError('Unknown structure query section')
    record = _load(structure, store_root)
    by_id = {node['node_id']: node for node in record['nodes']}
    selected = None
    if node_ids is not None:
        _array(node_ids, 'node filter', maximum=1024)
        for node_id in node_ids:
            _text(node_id, 'node filter ID')
        if len(set(node_ids)) != len(node_ids) or set(node_ids) - set(by_id):
            raise PocketError('Unknown or duplicate structure node filter')
        selected = set(node_ids)
    extra = {}
    if section == 'summary':
        if node_ids is not None:
            raise PocketError('Structure summary does not accept a node filter')
        rows = [{'label': record['label'], 'parent_structure': record['parent_structure'],
                 'counts': {'materials': len(record['materials']), 'nodes': len(by_id),
                            'relations': len(record['relations']),
                            'memberships': sum(n['note_count'] for n in record['nodes'])}}]
    elif section == 'nodes':
        rows = [_compact_node(node) for node in record['nodes'] if selected is None or node['node_id'] in selected]
    elif section == 'relations':
        rows = [r for r in record['relations'] if selected is None or r['from_node'] in selected or r['to_node'] in selected]
        extra['filter_scope'] = 'either_endpoint'
    else:
        if node_ids is None or len(node_ids) != 1:
            raise PocketError('Structure members requires exactly one node ID')
        node = by_id[node_ids[0]]
        rows = [{'note_id': key, 'member_index': index} for index, key in enumerate(node['note_ids'])]
        extra['node'] = {key: node[key] for key in ('node_id', 'material', 'material_revision', 'clip_id',
                                                  'space', 'span_qn', 'selection_sha256', 'note_count')}
    identity = digest({'structure': structure['sha256'], 'section': section,
                       'node_ids': sorted(selected) if selected is not None else None})
    offset = 0
    if cursor is not None:
        _text(cursor, 'material-structure cursor', 200)
        try:
            token, position = cursor.split(':')
            if not position.isascii() or not position.isdigit():
                raise ValueError('invalid offset')
            offset = int(position)
        except (ValueError, AttributeError) as error:
            raise PocketError('Malformed material-structure cursor') from error
        if token != identity or not 0 <= offset <= len(rows):
            raise PocketError('Stale material-structure cursor or incompatible filter')
    def envelope(page):
        following = offset + len(page)
        return receipt(artifacts={'structure': structure}, coverage=copy.deepcopy(COVERAGE),
                       result_schema='pocket.material-structure-query/v1', section=section, rows=page,
                       total=len(rows), returned=len(page), omitted=len(rows) - following,
                       next_cursor=f'{identity}:{following}' if following < len(rows) else None, **extra)
    page = []
    for row in rows[offset:offset + limit]:
        if len(canonical_bytes(envelope([*page, row]))) > max_bytes:
            break
        page.append(row)
    if offset < len(rows) and not page:
        raise PocketError('One structure query record exceeds byte budget; increase budget or narrow section')
    result = envelope(page)
    if len(canonical_bytes(result)) > max_bytes:
        raise PocketError('Structure query metadata exceeds byte budget')
    return result
