"""Validate observed reverse-proxy addresses against uvicorn's actual trust setting."""

import ipaddress


def untrusted_proxy_addresses(addresses: list[str], allow: str) -> list[str]:
    """Return source IPs uvicorn would ignore; never broaden the allow-list automatically."""
    networks = [ipaddress.ip_network(item.strip(), strict=False) for item in allow.split(",") if item.strip()]
    untrusted = []
    for value in addresses:
        address = ipaddress.ip_address(value)
        if address.is_unspecified:
            continue  # a listen address, not a proxy source
        if not any(address in network for network in networks):
            untrusted.append(value)
    return sorted(set(untrusted))
