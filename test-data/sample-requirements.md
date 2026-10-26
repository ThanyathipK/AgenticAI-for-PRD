# Appointment Booking Portal

## Objective

Give patients a self-service way to find, book, reschedule, and cancel clinic appointments.

## Functional requirements

### FR-01 — Search available appointments

Patients must be able to search by:

- specialty;
- clinic location;
- practitioner;
- date range.

Search results must show the practitioner, clinic, start time, duration, and whether the visit is in person or virtual. Results should appear within **2 seconds** for at least 95% of searches.

### FR-02 — Book an appointment

An authenticated patient can reserve one available slot. The system must prevent two patients from booking the same slot, even when their requests arrive at the same time.

Acceptance criteria:

1. Given an available slot, when a patient confirms the booking, then the appointment is created and a confirmation number is displayed.
2. Given a slot that was just booked by someone else, when a patient confirms, then no appointment is created and the patient is asked to choose another slot.
3. A booking confirmation email must be queued within 30 seconds.

### FR-03 — Reschedule or cancel

Patients may reschedule or cancel an appointment until 24 hours before its start time. Inside the 24-hour window, the portal must explain that the patient needs to contact the clinic.

When an appointment is rescheduled, its original confirmation number should remain unchanged and an audit event must record the old slot, new slot, patient ID, and timestamp.

### FR-04 — Reminders

The system should send reminders 24 hours and 2 hours before an appointment. A patient who has opted out of reminders must not receive them.

## Roles and permissions

| Role | Permission |
| --- | --- |
| Patient | Manage only their own appointments |
| Receptionist | Manage appointments for patients at their clinic |
| Administrator | View audit history and configure clinic hours |

## Privacy and reliability

- Personal and health data must be encrypted in transit and at rest.
- Audit history must be retained for 7 years.
- The monthly service availability target is 99.9%, excluding scheduled maintenance announced at least 48 hours in advance.
- Error messages shown to patients must not expose stack traces, database details, or another patient's information.

## Assumptions

- All dates and times are shown in the clinic's local timezone.
- Practitioners publish their availability through the existing scheduling system.

## Open questions

- Can a receptionist override the 24-hour cancellation restriction?
- How long should an unconfirmed slot be held during checkout?

## Out of scope for v1

- Recurring appointments
- Insurance eligibility checks
- Wait-list automation
