; Joint34 (Mission 3 + Mission 4) mission-ordering scheduler domain.
; Code-owned: submitted unchanged every joint34 plan revision; the per-revision
; numeric evidence lives only in the generated problem file.
;
; One drone (single core) serves at most one block of each mission per
; revision. Serving a mission moves the drone to that mission's block site and
; charges travel plus service time; deferring a mission skips its low-urgency
; block at the revision's defer price, which is what makes the ordering
; urgency-sensitive in plain STRIPS + action costs. Each serve action charges
; its own precomputed cost function because the solver's increase effect takes
; a plain numeric term, not arithmetic.
(define (domain joint34-scheduler)
  (:requirements :strips :action-costs)
  (:types mission location)
  (:constants
    m3 m4 - mission
    drone m3-site m4-site - location
  )
  (:predicates
    (at ?l - location)
    (served ?m - mission)
    (pending ?m - mission)
    (deferred ?m - mission)
    (completed ?m - mission)
  )
  (:functions
    (total-cost)
    (defer-cost ?m - mission)
    (serve-m3-from-drone-cost)
    (serve-m3-from-m3-site-cost)
    (serve-m3-from-m4-site-cost)
    (serve-m4-from-drone-cost)
    (serve-m4-from-m3-site-cost)
    (serve-m4-from-m4-site-cost)
  )

  (:action serve-m3-from-drone
    :parameters ()
    :precondition (and (pending m3) (at drone))
    :effect (and (served m3) (completed m3) (not (pending m3))
                 (at m3-site) (not (at drone))
                 (increase (total-cost) (serve-m3-from-drone-cost)))
  )
  (:action serve-m3-from-m3-site
    :parameters ()
    :precondition (and (pending m3) (at m3-site))
    :effect (and (served m3) (completed m3) (not (pending m3))
                 (increase (total-cost) (serve-m3-from-m3-site-cost)))
  )
  (:action serve-m3-from-m4-site
    :parameters ()
    :precondition (and (pending m3) (at m4-site))
    :effect (and (served m3) (completed m3) (not (pending m3))
                 (at m3-site) (not (at m4-site))
                 (increase (total-cost) (serve-m3-from-m4-site-cost)))
  )
  (:action serve-m4-from-drone
    :parameters ()
    :precondition (and (pending m4) (at drone))
    :effect (and (served m4) (completed m4) (not (pending m4))
                 (at m4-site) (not (at drone))
                 (increase (total-cost) (serve-m4-from-drone-cost)))
  )
  (:action serve-m4-from-m3-site
    :parameters ()
    :precondition (and (pending m4) (at m3-site))
    :effect (and (served m4) (completed m4) (not (pending m4))
                 (at m4-site) (not (at m3-site))
                 (increase (total-cost) (serve-m4-from-m3-site-cost)))
  )
  (:action serve-m4-from-m4-site
    :parameters ()
    :precondition (and (pending m4) (at m4-site))
    :effect (and (served m4) (completed m4) (not (pending m4))
                 (increase (total-cost) (serve-m4-from-m4-site-cost)))
  )
  (:action defer-m3
    :parameters ()
    :precondition (and (pending m3))
    :effect (and (deferred m3) (completed m3) (not (pending m3))
                 (increase (total-cost) (defer-cost m3)))
  )
  (:action defer-m4
    :parameters ()
    :precondition (and (pending m4))
    :effect (and (deferred m4) (completed m4) (not (pending m4))
                 (increase (total-cost) (defer-cost m4)))
  )
)
