# Task2 fix round1 scoped re-review

Base36fd0ad155b617c5df21277a5e76c4203f6093a7 → head dccd99226cbad9202b1c528ec1b10ee6dff50ac9.
Spec: changes required. Quality: changes required. Original I1–I4 addressed; one new Important R1. Internal review, not formal Claude approval.

I1: identity_daily_gate.py:79 combines pointers and eligible explicit links; :276,:309 reject same-source multiplicity. Actual-entry test :753 covers original collision.
I2: :239 blocks identity/current-snapshot conflict even with historical explanations. Generic FD900s diagnostic remains separate. Actual-entry test :822.
I3: :141 checks numbered current participants, :1137 fixed recent catalogue, :1198 supplementary pages. Tests :873 cover unknown/current reappearing historical IDs.
I4: identity_gate_evidence.py:300 validates recorded replay options; :364 capture/import/export clocks; :411 all COUNT_KEYS through unchanged real importer; :414 state equality. Tests :1114 genuine retained/absorbed/assumed, :1134 forged counters including repeat, :1158 invocation/clock guards.

## New Important R1
Locations identity_gate_evidence.py:216,:218(outcome), :226(transitions), identity_daily_gate.py:440–444(_materialized_original).
The fix allows multiple owners if source prefixes differ, sorts the direct APIF row first, but does not validate other owners' league, oriented team IDs, clock or score. _materialized_original checks only the selected tuple.

Counterexample: in separate recent before/after exports, valid APIF10 has projecthome1/away2 and originaltime/score; JC20 has home2/away1 and counterpart_id=apif:10. Both have validFK/results and unchanged values. Sources{apif,jc} pass; APIF10 is selected, transitionunchanged, only the correct row is validated.
Frozen archive I1 graph and separate current/repeat real-importer checks cannot validate these independent recent exports.
Fix every owner's identity/league/time/score against original/other owners before selecting one; keep legitimate retained+absorbed coexistence. Add actual-entry negative modifying only recent before/after exports while current/persistence evidence stays valid.

This is static control/dataflow evidence. Tiny no-write reproduction could not launch in exhausted container, was cancelled, and is NOT claimed successful. No suite rerun. Original review completed before CI success; parent subsequently verified hosted CI43637990303 success on exacthead. Tests passing does not resolve this finding.
