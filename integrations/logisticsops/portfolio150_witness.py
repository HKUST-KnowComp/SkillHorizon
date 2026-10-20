"""Reuse actual public receipts and update observed resource state after own writes."""

from copy import deepcopy
from .freight_witness import run as legacy_run

REVISION = "public_logistics_cache_v4"


async def run(call):
    cache={}
    clearances={}
    clearance_inventory_known=False
    def key(name,args):
        return name,tuple(sorted(args.items()))
    creations={"issue_connection_clearance":("read_clearance","clearance_id"),
        "release_through_baggage":("read_baggage_release","release_id"),
        "schedule_airport_transfer":("read_airport_transfer","trip_id"),
        "cancel_airport_transfer":("read_airport_transfer","trip_id"),
        "schedule_baggage_forwarding":("read_baggage_forwarding","forwarding_id"),
        "cancel_baggage_forwarding":("read_baggage_forwarding","forwarding_id")}
    cached_reads={"read_task","read_skill_file","get_flight_status","get_seat_map",
        "read_clearance","read_baggage_release","read_airport_transfer","read_baggage_forwarding"}
    bookings={}
    async def observed(name,**args):
        nonlocal clearance_inventory_known
        if name=="read_transfer_network":
            # Merge only immutable definitions already returned publicly with
            # the freshly observed dynamic inventory; no assumed empty state.
            public=cache[key("read_task",{})]
            state=await call(name,include_static=False)
            network=deepcopy(public["transfer_network"])
            availability={s["service_id"]:s["availability"] for s in state.pop("service_availability")}
            network.update(state)
            for service in network["freight_services"]:
                service["availability"]=availability[service["service_id"]]
            return network
        if name=="list_clearances" and clearance_inventory_known:
            rows=list(clearances.values());offset=args.get("offset",0);limit=args.get("limit",30)
            return {"records":deepcopy(rows[offset:offset+limit]),"total":len(rows)}
        if name in cached_reads and key(name,args) in cache:
            return deepcopy(cache[key(name,args)])
        previous=bookings.get(args.get("booking_id"))
        result=await call(name,**args)
        if name=="list_clearances":
            for row in result["records"]:clearances[row["clearance_id"]]=deepcopy(row)
            clearance_inventory_known=len(clearances)==result["total"]
        if name=="issue_connection_clearance":clearances[result["clearance_id"]]=deepcopy(result)
        if name in cached_reads: cache[key(name,args)]=deepcopy(result)
        if name in creations:
            read,id_key=creations[name]
            record=deepcopy(result)
            # A one-time notification is not part of the stored trip record.
            record.pop("operations_bulletin",None)
            cache[key(read,{id_key:result[id_key]})]=record
        if name=="get_booking": bookings[args["booking_id"]]=deepcopy(result)
        if name=="change_booking":
            # Public kernel reserves a seat in inventory on booking change.
            for fid,delta in [(previous["flight_id"],1),(args["new_flight_id"],-1)]:
                k=key("get_flight_status",{"flight_id":fid})
                if k in cache: cache[k]["available_seats"]["economy"]+=delta
            bookings[args["booking_id"]]=deepcopy(result)
        if name=="assign_seat":
            k=key("get_seat_map",{"flight_id":result["flight_id"],"seat_class":"economy","status":"available"})
            if k in cache:
                cache[k]["seats"]=[s for s in cache[k]["seats"] if s["seat_number"]!=args["seat_number"]]
        return deepcopy(result)
    await legacy_run(observed,skill_protocol="skill_declaration_v1",max_calls=100000,
                     efficient_reads=True,load_order=True,demand_flights=True)
