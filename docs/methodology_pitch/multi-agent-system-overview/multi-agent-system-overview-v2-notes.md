# Updated system overview

Generated with the built-in image-generation tool. The original PNG is preserved;
`multi-agent-system-overview-v2.png` is the updated three-agent presentation asset.

## Presentation semantics

At the user's direction, perception is presented as a functional agent alongside
Hyper Planning and Maneuver Control. The three AGENT badges do not imply that all
three are LLMs. World Model, Belief Update Manager and Context Coordination remain
supporting components. Continual learning is not depicted.

The drawing is a high-level conceptual overview, not a claim that every optional
sensor integration has passed end-to-end live validation. In particular, the
current Sukai runtime integration uses ideal visibility/pose observations and
keeps forecasts distinct from observed events. It does not manufacture Mission 1
report checks. Reporting-reliability beliefs update from report-check evidence;
raw observations alone do not establish reporting corruption.

## Code inspected

- Agent: `src/onr/runtime/composition.py`, `src/onr/application/context_coordination.py`,
  `src/onr/application/reporting_reliability.py`, role system prompts, and
  `src/onr/viewer/web/app/workflow.js`.
- Physical Runtime: `src/onr_physical_runtime/perception.py`,
  `src/onr_physical_runtime/runtime.py`, `src/onr_physical_runtime/agent/service.py`,
  and `src/onr_physical_runtime/world_model/model.py`.
- Solution: `sukai_interface/runtime_observation.py`, `sukai_interface/README.md`,
  and `ship_tracking_demo/perception.py`.

The viewer's generic belief-observations illustration was not treated as the
current Mission 1 reliability ingestion contract. The updated image emphasizes
role-specific context: planning context for Hyper, live context for Maneuver.
Arrows summarize information flow rather than software transport boundaries.

## Generation prompt

Use case: infographic-diagram / productivity-visual.
Asset: an updated executive presentation slide, 16:9 landscape, ideally 3840x2160, crisp readable text.
Input image is an OUTDATED SYSTEM OVERVIEW TO REDESIGN. Keep its broad recognizable arrangement: environment at far left; perception upper left and maneuver lower left; a large context/world-model region in the middle; planning agent on the right; operator above. Replace all outdated content with the exact current labels below. This is an updated version, not an annotation of the old screenshot.

Design: polished restrained academic/technical leadership slide, white background, ample whitespace, flat vector-like clean boxes and simple line icons, sophisticated dark navy typography with teal accents. Two agent boxes have strong matching navy headers and small "LLM AGENT" badges, so their collaboration is unmistakable. Supporting components use pale neutral or teal fills without agent badges. Remove red outlines, TC1/TC2/TC3/TC4 labels, thumbs-up/down, continual learning, anomaly-learning loops and old content. No code, repo names, formulas, numeric timings, acronyms such as FSM/API, implementation details, unsupported capabilities, 3D objects or decoration. No tiny text. All text must be readable, correctly spelled and verbatim as specified.

Title top left: "Multi-Agent Mission System"
Subtitle smaller: "Coordinated planning and action, grounded in evidence"

Above the main system container, a compact operator box:
"Operator"
"Mission goals • Progress & oversight"

At far left OUTSIDE main container, tall narrow box:
"Environment"
Use simple unobtrusive maritime ship/drone line icon.

Upper left INSIDE main container:
"Perception & Tracking"
"Observe ships and events"
Simple camera/ship observation icon. NOT an LLM agent.

Lower left INSIDE main container, visually emphasized agent card:
badge "LLM AGENT"
"Maneuver Control Agent"
"Assess live conditions"
"Navigate • Observe • Pursue"

Large middle region INSIDE main container:
heading "World Model & Context"
Three neat clearly separate supporting cards, stacked:
1. "World Model"
   "Objects • Events • Visibility"
2. "Belief Update Manager"
   "Reporting reliability • Uncertainty"
3. "Context Coordination"
   "Right context for each agent"
Use small simple map, probability/chart, and organized-layers icons if useful.
These are supporting components, not independent LLM agents. The world model supplies evidence to the belief manager; world evidence and updated beliefs feed context coordination. Use a simple downward evidence flow and a bypass from World Model to Context Coordination if spacing permits. No continual-learning or model-training implication.

Right INSIDE main container, visually emphasized matching agent card:
badge "LLM AGENT"
"Hyper Planning Agent"
"Interpret mission goals"
"Build and verify plans"
"Replan as evidence changes"

Arrow semantics MUST be correct, route connectors through clear whitespace, never over text:
- Environment -> Perception & Tracking, label "Observations".
- Perception & Tracking -> central World Model, label "Scene evidence".
- Context Coordination -> Hyper Planning Agent, label "Planning context".
- Context Coordination -> Maneuver Control Agent, label "Live context".
- Hyper Planning Agent -> Maneuver Control Agent via a clean bottom route, label "Plans & priorities".
- Maneuver Control Agent -> Hyper Planning Agent via a separate parallel bottom route, label "Feedback & replan requests".
- Maneuver Control Agent -> Environment, label "Actions".
- Environment -> Maneuver Control Agent, a distinct parallel connector labelled "Execution feedback".
- Operator -> Hyper Planning Agent, label "Mission goals", with a paired reverse return labelled "Progress". Route above the system, away from other arrows.
Keep the two collaboration arrows visually clear and central to the story, even though routed below the middle box. Do not imply that perception or the belief manager chooses physical actions. Do not imply both agents receive identical belief data: their two context arrows are deliberately labelled differently.

Main system container has a very pale neutral background and a fine border, with caption "Coordinated Multi-Agent System" along its bottom edge. Prioritize an elegant readable high-level overview over density; allow generous extra spacing in the connector lanes. Render one finished slide, no explanatory text outside it.

## Final edit prompt (user's three-agent clarification)

Edit this presentation diagram, preserving its layout, title, typography, colors, supporting components and all existing labels except the precise changes below. The user wants EXACTLY THREE agents, treating the perception module as an agent too.

1. Convert the upper-left "Perception & Tracking" card to the same agent-card visual language as Hyper and Maneuver: a navy header band with a pale teal pill badge reading exactly "AGENT". Rename the card title exactly "Perception Agent". Its subtitle must be "Observe ships and events". Retain its observation icon. Fit this comfortably within the upper-left card without overlap.
2. Change BOTH other agent badges from "LLM AGENT" to "AGENT". Leave the titles "Hyper Planning Agent" and "Maneuver Control Agent" unchanged. There must now be exactly three equally prominent AGENT badges, one per agent card. This is a functional multi-agent overview; do not describe perception as an LLM.
3. Fix ONLY the two top Operator/Hyper connectors so their arrowheads clearly show: "Mission goals" runs FROM Operator TO Hyper Planning Agent, with its single arrowhead at Hyper; "Progress" runs FROM Hyper Planning Agent TO Operator, with its single arrowhead at Operator. Remove any opposite-pointing extra arrowhead. Keep these as two separate clean orthogonal paths and put each existing label on its own path without ambiguity.
Everything else unchanged. Keep World Model, Belief Update Manager and Context Coordination as supporting components with no agent badge. Preserve the central hierarchy, context arrows, observation loop, actions/execution feedback and the paired Plans & priorities / Feedback & replan requests arrows. White background, slide-quality crisp text, 16:9 landscape. No extra technical details or continual learning.
