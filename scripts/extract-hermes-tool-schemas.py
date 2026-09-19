"""Extract pinned base schemas as data without importing or running Hermes."""
import ast
import hashlib
import json
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / 'work/hermes-pstack-image-context/hermes'
ROOT = Path(__file__).resolve().parents[1]


def assignments(path):
    return {node.targets[0].id: node.value for node in ast.parse(path.read_text()).body
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)}


def value(node, bindings):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return bindings[node.id]
    if isinstance(node, ast.Dict):
        return {value(k, bindings): value(v, bindings) for k, v in zip(node.keys, node.values)}
    if isinstance(node, (ast.List, ast.Tuple)):
        return [value(v, bindings) for v in node.elts]
    if isinstance(node, ast.JoinedStr):
        return ''.join(str(value(v.value if isinstance(v, ast.FormattedValue) else v, bindings))
                       for v in node.values)
    raise ValueError('non-data AST expression refused')


common = SOURCE/'tools/file_operations_common.py'
files = SOURCE/'tools/file_tools.py'
terminal = SOURCE/'tools/terminal_tool.py'
manifest_bytes = (SOURCE.parent/'source-manifest.json').read_bytes()
assert hashlib.sha256(manifest_bytes).hexdigest() == 'f1a952c642272353f2a41377cde3021be08e2689dc92fbb4ff356cc79e58cad3'
manifest = json.loads(manifest_bytes)
assert manifest['hermes_commit'] == '3b0e392e5a6922034feccac5771041ac78467757'
for path in (common, files, terminal):
    key = 'hermes/'+str(path.relative_to(SOURCE))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest['files_sha256'][key]
common_nodes, file_nodes, terminal_nodes = map(assignments, (common, files, terminal))
bindings = {'DEFAULT_READ_LIMIT': value(common_nodes['DEFAULT_READ_LIMIT'], {})}
foreground = terminal_nodes['FOREGROUND_MAX_TIMEOUT']
assert isinstance(foreground, ast.Call) and isinstance(foreground.func, ast.Name)
assert foreground.func.id == '_safe_parse_import_env'
assert ast.literal_eval(foreground.args[0]) == 'TERMINAL_MAX_FOREGROUND_TIMEOUT'
bindings['FOREGROUND_MAX_TIMEOUT'] = ast.literal_eval(foreground.args[1])
bindings['TERMINAL_TOOL_DESCRIPTION'] = value(terminal_nodes['TERMINAL_TOOL_DESCRIPTION'], {})
schemas = [value(file_nodes[n], bindings) for n in
           ('READ_FILE_SCHEMA', 'WRITE_FILE_SCHEMA', 'PATCH_SCHEMA', 'SEARCH_FILES_SCHEMA')]
schemas.append(value(terminal_nodes['TERMINAL_SCHEMA'], bindings))
tools = [{'type': 'function', 'function': s} for s in schemas]
payload = (json.dumps(tools, indent=2, ensure_ascii=True)+'\n').encode()
fixture = ROOT/'tests/fixtures/hermes/base-tool-schemas.json'
fixture.write_bytes(payload)
receipt = {'hermes_commit': '3b0e392e5a6922034feccac5771041ac78467757',
           'method': 'AST data extraction; no imports or calls executed',
           'source_manifest_verified': True,
           'scope': 'base file+terminal schemas, source-default foreground timeout; dynamic overrides unproven',
           'bindings': {'DEFAULT_READ_LIMIT': bindings['DEFAULT_READ_LIMIT'],
                        'FOREGROUND_MAX_TIMEOUT': bindings['FOREGROUND_MAX_TIMEOUT']},
           'source_sha256': {str(p.relative_to(SOURCE)): hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in (common, files, terminal)},
           'fixture_sha256': hashlib.sha256(payload).hexdigest(),
           'tools': [s['name'] for s in schemas],
           'description_lengths': {s['name']: len(s['description']) for s in schemas}}
(ROOT/'evidence/hermes-base-tool-schemas.json').write_text(json.dumps(receipt, indent=2)+'\n')
print(json.dumps(receipt))
