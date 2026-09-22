"""Run with an installed Pocket MIDI extra; outputs stay in a new local directory."""
import json
import tempfile
from pathlib import Path

from pocket_music import material_import, midi_export, midi_expression_plan

inputs = Path(__file__).resolve().parent
output = Path(tempfile.mkdtemp(prefix='pocket-expression-'))
store = str(output / 'store')
material = json.loads((inputs / 'material.json').read_text())
configuration = json.loads((inputs / 'configuration.json').read_text())
original = material_import(source={'kind': 'material', 'material': material},
                           store_root=store, request_id='external')
planned = midi_expression_plan(material=original['material'], store_root=store,
                               request_id='plan', **configuration)
composed = midi_export(material=original['material'], expression=planned['plan'],
                       store_root=store, request_id='composed', output_path=str(output / 'composed.mid'))
direct = midi_export(material=material, expression=configuration,
                     store_root=store, request_id='direct', output_path=str(output / 'direct.mid'))
assert direct['midi'] == composed['midi'] and direct['sidecar'] == composed['sidecar']
report = {'output': str(output), 'original': original, 'planned': planned,
          'composed': composed, 'direct': direct, 'native_verified': False, 'listening': 'not_performed'}
(output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
