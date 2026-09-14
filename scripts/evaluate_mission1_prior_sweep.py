"""Compare diagnostic priors on one fixed public snapshot with real MiniZinc.

This checks planner/oracle decisions, not achieved native-camera recall.
"""
import argparse
import json
from pathlib import Path

from evaluate_mission1_plan import solve_public_plan

from onr.contracts.reporting_reliability import ReportingReliabilitySnapshot


def main():
    p = argparse.ArgumentParser(__doc__)
    for name in ('agent-var', 'environment', 'priors', 'model', 'minizinc', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    a = p.parse_args()
    if a.agent_var.name != 'var' or not a.output.resolve().is_relative_to(a.agent_var.resolve()):
        p.error('output must be under caller-provided Agent var')
    a.output.mkdir(parents=True, exist_ok=False)
    environment = json.loads(a.environment.read_text())
    reports = {r['report_id']: r for r in environment['static_info']}
    results = []
    for bundle in sorted(a.priors.iterdir()):
        manifest = json.loads((bundle / 'manifest.json').read_text())
        assert manifest['diagnostic'] is True
        belief = ReportingReliabilitySnapshot.from_dict(json.loads((bundle / 'belief.json').read_text()))
        assert not environment['world_model_info']['event_report_checks']
        solution, timing = solve_public_plan(environment, belief, a.model, a.minizinc, a.output / bundle.name)
        row = {'case': bundle.name, 'flattening': manifest['flattening'], **timing,
               'prior_means': {s.entity_id: s.mean for s in belief.ships},
               'combined_score': solution['combined_score'] / solution['score_scale'],
               'selected': [{
                   'mode': x['surveillance_mode'], 'entity_id': x['entity_id'],
                   'start_s': x['start'] / x['time_scale'], 'duration_s': x['duration'] / x['time_scale'],
                   'report_entities': sorted({reports[r]['entity_id'] for r in x['parameters']['report_ids']}),
                   'utility': x['parameters']['utility'],
               } for x in solution['assignments']]}
        results.append(row)
        (a.output / 'summary.json').write_text(json.dumps({'diagnostic': True,
            'scope': 'Same public environment; only diagnostic prior varies. Not native recall.',
            'cases': results}, indent=2) + '\n')
        print(json.dumps(row), flush=True)


if __name__ == '__main__':
    main()
