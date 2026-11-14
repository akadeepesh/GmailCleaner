# Privacy Policy

_Last updated: 14 November 2026_

GmailCleaner is a free, open-source app that cleans up your Gmail inbox. It runs on your own computer (or on infrastructure you deploy yourself). This policy explains what data the app accesses, what it does with that data, and what it never does.

**The short version:** your email data stays on your machine. The GmailCleaner project has no servers, no accounts, no analytics and no telemetry. We never receive, store, sell or share your data.

---

## Who is responsible

GmailCleaner is maintained as an open-source project at [github.com/akadeepesh/GmailCleaner](https://github.com/akadeepesh/GmailCleaner). The maintainers do not operate any service that receives your data. The app talks directly from your computer to Google and, only in the cases described below, to a few other services.

## What the app accesses

When you sign in with Google, you grant GmailCleaner these Gmail permissions:

| Permission | Why the app needs it |
|---|---|
| `gmail.modify`: read, compose and modify your email | To scan your inbox and find newsletters, clutter and emails that need a reply, the app reads message metadata (sender, subject, date, labels, a short snippet and headers such as `List-Unsubscribe`). To carry out actions you confirm, it moves messages to Trash (where they stay recoverable for 30 days) and changes labels. The app never permanently deletes email. |
| `gmail.send`: send email on your behalf | To send your daily digest to your own inbox, and to send an unsubscribe email when a sender only supports unsubscribing by email (`mailto:`), and only for senders you confirm. |

The app takes action in Gmail only when you confirm it, or when it matches a trusted rule you wrote yourself.

## Where your data is stored

Everything is stored locally on your computer:

- **Google sign-in token:** saved as a file in the app's data folder (`%LOCALAPPDATA%\GmailCleaner` on Windows, `~/Library/Application Support/GmailCleaner` on macOS, `~/.local/share/GmailCleaner` on Linux, or the folder you choose).
- **Scan results, pending actions, history and statistics:** saved in a local database and report files in the same folder.
- **Gemini API key (optional):** saved in your operating system's keychain, or in a local file with restricted permissions if no keychain is available.

If you self-host GmailCleaner (for example on Google Cloud Run), this data lives in the storage you set up and control.

## What leaves your computer

GmailCleaner connects only to the following:

1. **Google (Gmail API and Google sign-in):** to read and act on your mailbox, as described above.
2. **Google Gemini (optional, off unless you add your own API key):** if you turn on AI triage or the AI-written digest, the app sends the **sender, subject, a snippet of up to 120 characters and the labels** of each message to the Gemini API, using **your own** API key. **The email body is never sent.** That data is processed under Google's terms for the Gemini API, which you accepted when you created the key.
3. **Unsubscribe links from senders you confirm:** to unsubscribe, the app opens the sender's own unsubscribe URL (a one-click POST or HTTP request) or sends the unsubscribe email the sender asked for. The sender learns that the address unsubscribed, as with any unsubscribe.
4. **GitHub (update check):** once a day, the desktop app asks GitHub's public API whether a newer GmailCleaner release exists. No personal data is sent, only a generic `GmailCleaner/<version>` user agent. You can turn this off under **Settings → App**.

Nothing else is sent anywhere. There is no analytics, crash reporting, advertising or tracking.

## Google API Services User Data Policy

GmailCleaner's use and transfer of information received from Google APIs complies with the [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy), including the Limited Use requirements. Specifically:

- Gmail data is used only to provide the inbox-cleanup features you can see in the app.
- Gmail data is not transferred to anyone else, except to the Gemini API when you turn on AI features with your own key (described above), as needed to provide those features, or as required by law.
- Gmail data is never used for advertising, never sold, and never used to train AI or machine-learning models by the GmailCleaner project.
- No person reads your Gmail data. The project has no access to it.

## Keeping or deleting your data

- **Sign out of a Gmail account** under **Accounts**. This revokes the app's access with Google and deletes its sign-in token. You can also choose to delete that account's local database and reports.
- **Delete everything** by quitting the app and deleting its data folder (listed above).
- **Revoke access at any time** from your Google Account at [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

Because the project never holds your data, there is nothing for us to delete on our side.

## Children

GmailCleaner is not directed at children under 13 and does not knowingly process their data.

## Changes to this policy

If this policy changes, the new version will be published in this file with a new "Last updated" date. The full history is visible in the repository's commit log.

## Contact

Questions or concerns? Open an issue at [github.com/akadeepesh/GmailCleaner/issues](https://github.com/akadeepesh/GmailCleaner/issues).
