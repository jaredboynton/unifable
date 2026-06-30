# Architecture

The system is split into three layers and dependencies flow downward only.

- transport layer: accepts requests and validates input
- domain layer: business rules, never imports the transport layer
- storage layer: persistence, owns the schema

The golden rule: the domain layer must not depend on transport or storage
concretions. Wire adapters at the edges.
