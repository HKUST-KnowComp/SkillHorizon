# Build a versioned bibliography

Join the selected canonical works across sections, once per work. Bring catalog metadata to the current
released source revision. Assign unique readable keys, accounting for same-surname/year collisions. The
optional scripts/prepare_keys.py prepares keys from supplied metadata only. Submit the exact paper-to-key
mapping to generate_bibtex. Its creation receipt contains identity and version/key bindings. Use
read_library_artifact to inspect the saved artifact: title, ordered authors, year, venue, DOI,
entry type and version bindings must refer to the intended works. The environment uses Pybtex to serialize
actual BibTeX bytes. A prior artifact is frozen and does not become current after later metadata updates.
