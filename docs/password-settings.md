# Password changes from settings

Admin, manager, chef, and customer settings pages include **Security → Change
password**. The inline form requires the current password, a new password, and
confirmation. Password changes require an explicit Update password action; they
are separate from automatic appearance and notification saves.

Customers signed in with Google do not see the password-change section. Session
and profile responses expose `can_change_password`; Google sessions receive
`false`, and direct password-change requests from those sessions return 403.
The standalone password-change page redirects them to settings.

The form enforces 8–128 characters with a letter and a number or symbol, matching
the server policy. Confirmation must match, and the new password must differ from
the current one. Password values stay in the form and request only; they are not
stored in the settings store, Redux, or browser storage. Cancelling or switching
accounts unmounts the fields, and a successful save clears them.

All four APIs use POST `/api/auth/{role}/change-password` and their existing role
authentication dependencies. Admin, manager, and customer accept
`current_password`, `new_password`, and `confirm_password`. Chef retains its
`password` field for compatibility and also accepts `confirm_password`. Normal
chef changes require the current password; the existing mandatory first-login
setup can still omit it.

Successful changes store a password hash, set `password_changed_at`, and renew the
HttpOnly session cookie. The settings form reconnects live updates using the
renewed cookie. Older session tokens are rejected. Customer sign-in
method metadata is preserved. Database failures roll back and return a safe 503
without replacing the session cookie. Existing account restrictions, origin
checks, and authentication rate limits apply.

The form links to each portal's existing password recovery page. Customers who
have no DineBook password yet can use that flow to set one.

No new database migration or dependency is required. From `DineBookBackend`, run
`venv/bin/python -m unittest tests.test_password_change -q`. From the frontend
`DineBook` directory, run `npm run test:password-change` and `npm run build`.
