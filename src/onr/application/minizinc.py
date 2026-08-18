"""Pure translation of temporal Mission Specifications to MiniZinc assets."""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType

from onr.contracts.planning import MissionSpec


_MODEL = (
    b"int: maneuver_count;\n"
    b"set of int: MANEUVERS = 1..maneuver_count;\n"
    b"int: horizon;\n"
    b"array[MANEUVERS] of string: maneuver_ids;\n"
    b"array[MANEUVERS] of int: durations;\n"
    b"array[MANEUVERS] of set of MANEUVERS: dependencies;\n"
    b"array[MANEUVERS] of var 0..horizon: start;\n"
    b"constraint forall(i in MANEUVERS)(start[i] + durations[i] <= horizon);\n"
    b"constraint forall(i in MANEUVERS, dependency in dependencies[i])(\n"
    b"  start[i] >= start[dependency] + durations[dependency]\n"
    b");\n"
    b"var 0..horizon: makespan;\n"
    b"constraint makespan = max(i in MANEUVERS)(start[i] + durations[i]);\n"
    b"solve :: int_search(start, input_order, indomain_min, complete)\n"
    b"  minimize makespan;\n"
    b"output [\n"
    b'  "{\\"assignments\\":[",\n'
    b"  concat([\n"
    b'    "{\\"maneuver_id\\":\\"" ++ maneuver_ids[i] ++\n'
    b'    "\\",\\"start\\":" ++ show(start[i]) ++\n'
    b'    ",\\"duration\\":" ++ show(durations[i]) ++ "}" ++\n'
    b'    (if i < maneuver_count then "," else "" endif)\n'
    b"    | i in MANEUVERS\n"
    b"  ]),\n"
    b'  "]}"\n'
    b"];\n"
)


def translate_minizinc(mission_spec: MissionSpec) -> Mapping[str, bytes]:
    """Render deterministic MiniZinc model and data assets."""

    maneuver_ids = [item.maneuver_id for item in mission_spec.maneuvers]
    indexes = {
        maneuver_id: index
        for index, maneuver_id in enumerate(maneuver_ids, start=1)
    }
    dependencies = []
    for maneuver in mission_spec.maneuvers:
        dependency_indexes = sorted(indexes[item] for item in maneuver.dependencies)
        dependencies.append("{" + ",".join(map(str, dependency_indexes)) + "}")

    rendered_ids = json.dumps(maneuver_ids, separators=(",", ":"))
    rendered_durations = ",".join(
        str(item.duration) for item in mission_spec.maneuvers
    )
    data = (
        f"maneuver_count = {len(maneuver_ids)};\n"
        f"horizon = {mission_spec.horizon};\n"
        f"maneuver_ids = {rendered_ids};\n"
        f"durations = [{rendered_durations}];\n"
        f"dependencies = [{','.join(dependencies)}];\n"
    ).encode("utf-8")
    return MappingProxyType({"model.mzn": _MODEL, "data.dzn": data})
