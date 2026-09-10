# Security and privacy

AI Time Guard is designed as a local-only utility. The report server binds to
`127.0.0.1`, and usage history, configuration, and logs stay under
`~/.ai-time-guard/`.

The app records durations and detected tool names. It does not record prompt
content, source code, keystrokes, or window contents. On macOS, process and
frontmost-app detection may require Accessibility permission.

The local API has no authentication. Do not expose port `19527` through a
reverse proxy, port-forward, tunnel, or public network interface. Any local web
page can potentially send a request to the API because CORS is enabled for
local integrations.

To report a vulnerability, open a private GitHub security advisory in this
repository. Please do not include personal usage history in a public issue.
