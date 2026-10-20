"""Validate existing domain scores without conflating execution diagnostics."""


def business_accepted(business: dict) -> bool:
    """Return an existing score's boolean; raise ValueError for unknown/invalid scores."""
    if not isinstance(business, dict) or ('valid' in business and business['valid'] is not True):
        raise ValueError('No valid domain evaluation is available')
    if 'business' in business or 'gateway' in business:
        domain, gateway = business.get('business'), business.get('gateway')
        if (not isinstance(domain, dict) or not isinstance(gateway, dict)
                or domain.get('valid') is not True or type(domain.get('task_success')) is not bool
                or gateway.get('sealed') is not True or gateway.get('execution_unknown') is not False
                or gateway.get('inflight') is not None):
            raise ValueError('PrivateGateway evaluation is not settled/valid')
        return domain['task_success']
    for key in ('accepted', 'task_success'):
        if type(business.get(key)) is bool:
            return business[key]
    raise ValueError('No boolean business evaluation is available')
