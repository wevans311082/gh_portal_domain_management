# Trusted client-IP contract — SEC-04

## Application configuration

`TRUSTED_PROXY_CIDRS` is an application setting populated from a comma-separated environment value. Its default is an empty list: the application uses the actual socket peer and ignores forwarding headers. No private network, localhost address or container network is automatically trusted.

Configure only addresses or canonical CIDRs belonging to controlled proxies that can connect directly to this application or appear as controlled forwarding hops. Prefer individual addresses or narrowly scoped networks. Do not include client networks or all-address ranges. Hostname lookup is not supported. IPv4-mapped IPv6 socket addresses normalize to IPv4, so configure those peers using IPv4 addresses/ranges.

For an illustrative chain with socket peer `192.0.2.10` and a second controlled proxy `192.0.2.11`, the corresponding setting would be `192.0.2.10/32,192.0.2.11/32`. These documentation addresses are examples, not this installation's configuration. IPv6 address/CIDR entries are also supported. `manage.py check` reports malformed configuration as `core.E001`; runtime handling fails closed if checks were skipped.

## Header algorithm

1. Validate the socket address. An invalid/missing socket address produces an unknown address.
2. If the socket peer is not explicitly trusted, return that peer and ignore all supplied forwarding headers.
3. If the peer is trusted and `X-Forwarded-For` is absent, return the peer. A proxy address must not be allowlisted as a substitute for its clients.
4. For a present header, require at most 4096 characters and 32 plain address entries. Reject empty entries, invalid addresses, ports, brackets, scope IDs and other syntax. A malformed chain produces an unknown address rather than trusting part of it.
5. Starting at the socket peer, consume entries right-to-left while the current hop is trusted. Stop at the first untrusted address. Entries further left cannot override it.

Example: trusted socket peer `.10`, header `forged-address, client-address, controlled-proxy-.11` resolves to `client-address`, provided all entries are valid addresses and `.11` is explicitly trusted. An untrusted `.11` would instead be the returned boundary. When every hop is trusted, the leftmost address is returned.

`Forwarded` and `X-Real-IP` are deliberately ignored. One unambiguous header contract is used throughout the application. Unknown addresses cannot pass an IP allowlist, share an unknown rate-limit bucket and are saved as NULL where address fields are nullable. This does not affect Django's separate host/scheme configuration.

## Required external verification

Application tests prove parsing and consumer consistency, not the real network boundary. Before relying on forwarded client identities, separately establish:

- Which peer address the application actually sees and which proxy hops are controlled.
- That each configured proxy strips or safely appends forwarding data according to this right-to-left contract.
- That unrelated workloads cannot reach the application while presenting a trusted peer identity.
- That requests through each supported path resolve to the expected real address and direct spoofed headers are ineffective.
- That staff allowlists contain intended client addresses rather than a shared proxy fallback address.

Until that verification and explicit configuration, multiple clients behind a proxy share its socket address for application throttling/audit. No Swarm, nginx, network, server or deployment configuration was modified. The historical infrastructure-dependent finding remains externally outstanding.
