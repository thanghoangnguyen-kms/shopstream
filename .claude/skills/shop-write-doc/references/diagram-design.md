# Diagram Design

Read this reference before adding or materially changing a diagram.

## 1. Decide whether a diagram earns its space

Use a diagram only when prose or a short table would hide an important relationship.

| Need                                                   | Diagram          |
| ------------------------------------------------------ | ---------------- |
| Three or more components and their boundaries          | Flowchart        |
| Calls, events, retries, acknowledgments, or time order | Sequence diagram |
| Lifecycle and valid transitions                        | State diagram    |
| Entities and cardinality                               | ER diagram       |
| One mapping or one short rule                          | No diagram       |

One diagram should answer one reader question. Link to a canonical figure when another document already owns the flow.

## 2. Choose the abstraction level

For integration documents, default to canonical high-level concepts:

- use stable service and team names;
- show system boundaries and ownership;
- label edges with a friendly transport or artifact name;
- show success, rejection, retry, or fallback only when it changes the integration contract.

Keep these details in prose or tables unless the diagram exists specifically to explain them:

- exact subjects, keys, bucket names, and environment variables;
- every payload field;
- credentials, secrets, internal hostnames, and private network details;
- pod, container, and library internals;
- duplicated stream configuration owned by a topology reference.

The diagram and prose must use the same canonical terms. A friendly label may clarify a component, but it must not invent a new component boundary.

## 3. Apply Shopstream diagram rules

Read the current diagram section in `docs/specs/guide/guide-doc-style.md`. Follow its exact caption, theme, arrow, naming, placement, and size rules.

In addition:

- keep diagrams under roughly 50 nodes;
- prefer top-down layout for broad integration maps;
- use left-to-right layout for short pipelines;
- keep sequence participants to the systems needed for that flow;
- label important connections with words, not color alone;
- use short labels that remain readable in the docs column;
- attach a caption that states what the reader should learn.

Do not commit rendered SVG or PNG previews. Inline Mermaid or ASCII remains the source of truth.

## 4. Cover integration gaps deliberately

For a cross-team integration TRD, check whether readers need these views:

1. Integration overview: ownership and major artifacts.
2. Configuration lifecycle: publish, validate, apply, retain, reject, and delete.
3. Runtime output: detect, publish, acknowledge, retry, enrich, store, and notify.

Add only the views that materially improve the document. Do not force all three into a simple design.

## 5. Render and inspect

Validation has two layers:

1. Syntax and site integration: run the repository docs build.
2. Visual quality: render a local preview with the available Mermaid tooling, then inspect it.

Check the preview for:

- clipping or overlapping labels;
- excessive width or height;
- unclear arrow direction;
- crossed edges that hide the main path;
- inconsistent names;
- unreadable text or color-only meaning;
- detail that belongs in a contract table.

If the overview is too wide, change orientation or split it by reader question. Apply the repository theme in the document even if the preview tool uses an approximate light theme.

## 6. Diagram review questions

- Does the diagram clarify a relationship the prose does not show quickly?
- Does every node correspond to a real or explicitly proposed boundary?
- Does the diagram avoid leaking irrelevant implementation detail?
- Does it agree with contract ownership and failure behavior?
- Can the intended partner team understand it without knowing internal code names?
- Does the rendered result fit the documentation layout?

Remove the diagram if these answers are weak.
