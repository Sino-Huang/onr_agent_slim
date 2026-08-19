(define (domain survey-return)
  (:requirements :strips :action-costs)
  (:predicates (surveyed) (returned))
  (:functions (total-cost))

  (:action survey
    :parameters ()
    :precondition (and)
    :effect (and (surveyed) (increase (total-cost) 5))
  )

  (:action return-to-base
    :parameters ()
    :precondition (and (surveyed))
    :effect (and (returned) (increase (total-cost) 2))
  )
)
