# Security

The controller has no generic shell surface and no mutation endpoint. External
programs are invoked with fixed argv arrays, `shell=False`, explicit timeouts,
bounded stdout and stderr, and reader-specific command allowlists.

Registered workspace roots are resolved before use. Candidate paths must remain
inside the resolved root, and symlinked roots or symlink escapes are rejected.
The controller never follows a registry's legacy-workspace field.

Known credential formats, authorization headers, private-key blocks, URL user
information, and runtime session keys are redacted from returned errors and
structured output. Raw command output is never rendered directly.

The Markdown renderer disables raw HTML and sanitizes its generated HTML. The
Wiki cache contains searchable document text and must remain private when the
knowledge source is private.

Binding is restricted to `127.0.0.1:3001`. Authentication and private network
exposure are deployment concerns and are intentionally absent from the MVP.

