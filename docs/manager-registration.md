# Manager email verification

The manager application shares email-verification proof across Personal Details,
Restaurant Info, Documents, Operating Hours, and Review. A boolean in the browser
does not authorize registration.

1. `POST /api/auth/manager/send-verification` sends an OTP valid for two minutes.
2. `POST /api/auth/manager/verify-email` checks the OTP and returns `email`,
   `verification_token`, and `expires_at`. The random token is bound to that email
   and valid for two hours from successful verification, independently of the OTP.
3. Every form step checks the shared token's email and expiry. The final multipart
   `POST /api/auth/manager/register` must include `verification_token`. The server
   checks its hash, email, and expiry before creating an account or uploading files.

Only the token hash is stored, using the existing verification record. Resending
an OTP replaces the record and revokes the previous token. Successful registration
deletes the record in the account transaction; a failed application rolls back so
the same token can be retried. Row locking serializes verification, resends, and
submission. The registration token cannot be used as a sign-in session.

The frontend holds the token in memory for the lifetime of the form. Changing the
email clears it and cancels pending OTP requests. Expiry returns to Personal
Details with the entered details, passwords, and selected files intact. A server
rejection with `detail.code = email_verification_required` does the same. Reloading
the page restarts the existing form and requires verification again.

No database migration is required. Run backend coverage with
`venv/bin/python -m unittest tests.test_manager_registration_verification -q`.
From the frontend directory, run
`node --test src/lib/managerRegistrationValidation.test.js` and `npm run build`.
