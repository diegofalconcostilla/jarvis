---
title: Jarvis
---

# Jarvis

Jarvis is a personal, **local-first** assistant that runs on its owner's own Mac. It is a single-user tool, not a
public service: there are no accounts, no sign-ups and no other users.

## What it does

- Reads the owner's **new** Gmail messages and Google Calendar events, **read-only**, to keep a private list of
  upcoming activities (appointments, reservations, trips, deadlines).
- Sends the owner short reminders: a weekly overview on Mondays and a reminder on the day of each activity.

## How it handles data

- All processing happens **on the owner's computer**, with a local AI model. Email content is never sent to any AI
  service or third party.
- Access is **read-only** (`gmail.readonly`, `calendar.readonly`): Jarvis cannot send, delete or change anything.
- Details: [Privacy Policy](privacy.html)

Source code: [github.com/diegofalconcostilla/jarvis](https://github.com/diegofalconcostilla/jarvis)
