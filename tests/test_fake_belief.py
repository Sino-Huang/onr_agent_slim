from onr.demo.fake_belief import create_fake_entity_risk_snapshot


def test_fake_entity_risks_are_deterministic_belief_manager_output() -> None:
    first = create_fake_entity_risk_snapshot("mission:demo")
    second = create_fake_entity_risk_snapshot("mission:demo")

    assert first == second
    assert first.belief_revision == 20
    assert first.input_event_id == "fake-event-risk:20"
    assert {item.key.entity_id for item in first.marginals} == {
        str(index) for index in range(1, 21)
    }
    assert len(first.marginals) == 20
    assert {item.key.risk_type for item in first.marginals} == {"event-risk"}
    assert all(0.0 <= item.probability_risk <= 1.0 for item in first.marginals)
    assert len({round(item.probability_risk, 3) for item in first.marginals}) > 1
