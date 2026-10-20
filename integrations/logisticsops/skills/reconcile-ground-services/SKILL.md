---
name: reconcile-ground-services
description: Preserve and transfer baggage and accessibility-service identities after itinerary changes.
---

# Reconcile ground services

Read the current booking revision and its original ground-service records. Match booking, passenger,
flight and revision before updating anything. Transfer each stale existing baggage/service record to
the current revision using its own identifier. This preserves tag identity and transfer history.
Do not attach another passenger's service, recreate an original bag, or treat a previous successful
transfer as proof that it is still current after another change. A current record needs no transfer.

Useful tools: get_booking, list_booking_services, transfer_baggage, transfer_special_service.
