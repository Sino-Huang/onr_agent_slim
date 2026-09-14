"""Prepare or install an explicitly labelled Mission 1 diagnostic prior.

Generation may read evaluator truth only because this is an oracle control.
Installation uses the existing belief store/outbox; no evidence checks or
planner decisions are fabricated. Ordinary launch behavior is unchanged.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from onr.application.reporting_reliability import (
    FileReportingReliabilityStore,
    ReportingReliabilityCheckpoint,
    ReportingReliabilityManager,
    ReportingReliabilityService,
)
from onr.contracts.reporting_reliability import ReportingReliabilitySnapshot


def prepare(truth: Path, output: Path, *, mission_id: str, flattening: float, created_at: str) -> None:
    source = json.loads(truth.read_text())
    probabilities = {int(k): float(v) for k, v in source['ship_corruption_probabilities'].items()}
    manager = ReportingReliabilityManager.with_diagnostic_prior(mission_id, probabilities, flattening=flattening)
    snapshot = manager.snapshot(input_event_id=manager.last_input_event_id, input_revision=0, created_at=created_at)
    output.mkdir(parents=True, exist_ok=False)
    documents = {
        'belief': snapshot.to_dict(),
        'checkpoint': manager.checkpoint().to_dict(),
        'manifest': {'diagnostic': True, 'mission_id': mission_id, 'flattening': flattening,
                     'vessel_count': len(probabilities),
                     'scope': 'Oracle-to-population prior control; not learned evidence',
                     'shared_omission_prior': 'Unchanged Beta(1,1)',
                     'truth_file': str(truth.resolve())},
    }
    for name, value in documents.items():
        (output / f'{name}.json').write_text(json.dumps(value, indent=2) + '\n')


def install(bundle: Path, storage_root: Path, *, mission_id: str) -> None:
    manifest = json.loads((bundle / 'manifest.json').read_text())
    snapshot = ReportingReliabilitySnapshot.from_dict(json.loads((bundle / 'belief.json').read_text()))
    checkpoint = ReportingReliabilityCheckpoint.from_dict(json.loads((bundle / 'checkpoint.json').read_text()))
    if manifest.get('diagnostic') is not True or manifest['mission_id'] != mission_id:
        raise ValueError('explicit diagnostic manifest for this Mission is required')
    if checkpoint.mission_id != mission_id or snapshot.mission_id != mission_id:
        raise ValueError('prior bundle belongs to another Mission')
    if checkpoint.belief_revision != 1 or checkpoint.last_input_revision != 0 or checkpoint.processed_check_ids:
        raise ValueError('diagnostic initialization requires an unevidenced revision-1 prior')
    manager = ReportingReliabilityManager.from_checkpoint(checkpoint)
    rebuilt = manager.snapshot(input_event_id=checkpoint.last_input_event_id, input_revision=0, created_at=snapshot.created_at)
    if rebuilt != snapshot:
        raise ValueError('prior snapshot does not match its checkpoint')
    store = FileReportingReliabilityStore(storage_root)
    if store.load(mission_id) is not None:
        raise ValueError('refusing to replace an existing belief state')
    store.save(snapshot, checkpoint, ReportingReliabilityService._pending(snapshot))
    (storage_root / 'diagnostic-prior.json').write_text(json.dumps(manifest, indent=2) + '\n')


def main() -> None:
    p = argparse.ArgumentParser(__doc__)
    p.add_argument('--agent-var', type=Path, required=True)
    commands = p.add_subparsers(dest='command', required=True)
    generate = commands.add_parser('generate')
    generate.add_argument('--truth', type=Path, required=True)
    generate.add_argument('--output', type=Path, required=True)
    generate.add_argument('--flattening', type=float, required=True)
    generate.add_argument('--mission-id', required=True)
    generate.add_argument('--created-at', required=True)
    seed = commands.add_parser('install')
    seed.add_argument('--bundle', type=Path, required=True)
    seed.add_argument('--storage-root', type=Path, required=True)
    seed.add_argument('--mission-id', required=True)
    a = p.parse_args()
    target = a.output if a.command == 'generate' else a.storage_root
    if a.agent_var.name != 'var' or not target.resolve().is_relative_to(a.agent_var.resolve()):
        p.error('outputs must remain under the caller-provided Agent var directory')
    if a.command == 'generate':
        prepare(a.truth, a.output, mission_id=a.mission_id, flattening=a.flattening, created_at=a.created_at)
    else:
        install(a.bundle, a.storage_root, mission_id=a.mission_id)
    print(json.dumps({'diagnostic': True, 'command': a.command, 'output': str(target)}))


if __name__ == '__main__':
    main()
