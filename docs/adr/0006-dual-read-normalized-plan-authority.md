# Dual-read Normalized Plan authority

Status: accepted

During the migration, a Normalized Plan carries exactly one authority form: either the legacy embedded Mission Specification or reference-only Plan Provenance. Legacy canonical documents and transport revision 1 remain readable; provenance documents use transport revision 2 and bind Mission Intent, Planning Decision, Operational Scene Graph, generated assets, and solver evidence through verifiable references. Consumers use the Normalized Plan's authority-neutral mission identity and source authority, allowing issue #45 to remove the legacy form without another transport redesign.
