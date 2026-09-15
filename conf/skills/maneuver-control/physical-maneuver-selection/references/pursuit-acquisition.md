# Pursuit acquisition and recovery

`pursue` tracks from partial observations and explores locally when the target is
unseen; it does not navigate to the planner's rendezvous. Keep the numeric target
ID and `pursue_ship` assignment through acquisition, tracking, and recovery.

1. Use current `visible_ship_ids`/`visible_ships` as sighting authority. GPS and
   report positions are search hints. Preserve a suitable active pursuit while
   tracking or through brief occlusion.
2. If unseen and the drone has not reached
   `desired_outcome.acquisition_rendezvous.location`, navigate there immediately
   with its arrival deadline and the drone's flight altitude. Preserve progressing
   navigation while unseen; an exact current target sighting requires switching
   to pursuit before arrival in that heartbeat.
3. On rendezvous navigation completion, call `pursue` in the same heartbeat even
   if still unseen. Arrival starts local exploration; it does not satisfy the
   assignment or justify holding through the evidence window.
4. Anchor the initial search bound to pursuit start: the first required
   observation time after start, or the next GPS broadcast after start, whichever
   is earlier; use window end only when neither exists. A passed bound stays
   passed. Mission wall-clock reasoning consumes no search budget.

Use `derived_pursuit_facts` rather than recomputing these times. A nonnegative
`seconds_since_acquisition_bound` means the bound has passed.

After unsuccessful acquisition, or after tracking loss when new position evidence
materially changes the search area, inspect the newest target GPS fix. A usable fix
sampled after the failed search began supports recovery navigation to its x/y at
the drone's flight altitude. Include source/sample time in reflection/action ID,
notify Hyper of the missed acquisition, then resume pursuit on arrival or
sighting. Do not retry the same fix merely because the environment payload is
newer.

If no newer usable fix exists, send a Hyper replan/evaluation request and issue no
physical command. Keep the Transition Intent and existing command unchanged;
do not claim cancellation, acquisition, or report verification. Reassess on a
fresh fix, sighting, or revised plan. Use the supplied GPS interval; 30 seconds is
not universal and some profiles provide no GPS.

Example: target 23 is unseen, rendezvous `(180,-20)` is due at t52, and the drone
is at `(40,-20,-25)` at t40. Navigate to `(180,-20,-25)` by t52. If navigation
finishes at t46, pursue target 23 immediately. If that search misses its bound and
a newer t60 fix locates the target at `(150,60)`, navigate to `(150,60,-25)` and
notify Hyper. Without that newer fix, notify Hyper without a replacement action.
