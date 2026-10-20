"""Extract native local-goal completion and task success for new executions."""


def native_goal(environment, business):
    """Return local goal counts and the independent full business verdict."""
    specs = {
        'SocialOps': ('brief', (), 'completed_briefs', 'required_briefs', 'briefs', 'accepted'),
        'LogisticsOps': ('network_booking', ('network_evaluation',), 'completed_bookings', 'required_bookings', None, None),
        'ResearchOps': ('review_round', (), 'completed_rounds', 'required_rounds', 'rounds', 'accepted'),
        'DocOps': ('page', (), 'completed_pages', 'required_pages', 'pages', 'accepted'),
        'FactoryOps': ('quantity_complete_work_order', (), 'completed_orders', 'required_orders', 'orders', 'quantity_complete'),
        'FinanceOps': ('work_package', (), 'completed_work_packages', 'total_work_packages', None, None),
        'FieldOps': ('restored_device', ('business', 'state'), 'completed_cases', 'total_cases', 'cases', 'success'),
    }
    unit, path, done, total, objects, flag = specs[environment]
    values = business
    for name in path:
        values = values[name]
    completed, required = values[done], values[total]
    assert type(completed) is int and type(required) is int
    assert 0 <= completed <= required and required > 0
    if objects:
        items = values[objects]
        items = list(items.values()) if isinstance(items, dict) else items
        assert len(items) == required and all(type(item[flag]) is bool for item in items)
        assert sum(item[flag] for item in items) == completed
    accepted = (business['task_success'] if environment == 'FinanceOps'
                else business['business']['task_success'] if environment == 'FieldOps'
                else business['accepted'])
    assert type(accepted) is bool
    return {'unit': unit, 'completed': completed, 'required': required,
            'fraction': completed / required, 'accepted': accepted}
