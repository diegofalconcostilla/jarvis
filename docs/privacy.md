---
title: Jarvis Privacy Policy
---

# Privacy Policy

_Last updated: October 6, 2026_

Jarvis is a personal assistant used by **one person, its owner**, on their own computer. This policy explains how it
handles data from Google services.

## Data accessed

With the owner's explicit consent, Jarvis requests these Google permissions, both **read-only**:

- `https://www.googleapis.com/auth/gmail.readonly`: to read **new** email messages and find dated activities
  (appointments, reservations, travel, deadlines). Promotional and social email is skipped.
- `https://www.googleapis.com/auth/calendar.readonly`: to read upcoming calendar events.

Jarvis **cannot** send, delete, modify or label email, and cannot create or change calendar events.

## How the data is used

- Messages are processed **locally on the owner's computer** by a local AI model, only to extract activity details:
  title, date, time and place.
- Only those extracted details are stored, in a private file on the owner's computer. Full email content is not
  stored.
- The owner receives short reminders containing only an activity's title, time and place, delivered through the
  owner's own messaging bot.

## Sharing

Google user data is **not** sold, shared, transferred or disclosed to any third party, and is **not** used for
advertising or to train AI models. Email content is never sent to any cloud AI service.

Jarvis's use and transfer of information received from Google APIs adheres to the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

## Security

The Google access token and the activity list are stored only on the owner's computer, readable only by the owner's
account, and are never committed to source control.

## Retention and deletion

Activities are kept until they have passed. The owner can delete all stored data at any time by deleting the local
data folder, and can revoke Jarvis's access at any time at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions).

## Contact

Questions about this policy: open an issue at
[github.com/diegofalconcostilla/jarvis/issues](https://github.com/diegofalconcostilla/jarvis/issues).
