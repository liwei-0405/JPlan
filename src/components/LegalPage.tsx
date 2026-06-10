import React from "react";
import { Link } from "react-router-dom";
import { jplanLogoUrl } from "../brand";

const updatedAt = "June 10, 2026";

function LegalShell({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <main className="min-h-screen bg-slate-50 px-4 py-10 text-slate-900">
      <div className="mx-auto max-w-3xl rounded-2xl border border-slate-200 bg-white p-6 shadow-sm sm:p-8">
        <Link to="/" className="mb-8 inline-flex items-center gap-2 text-sm font-medium text-primary">
          <img src={jplanLogoUrl} alt="JPlan logo" className="brand-logo-nav rounded-md object-cover shadow-sm" />
          JPlan
        </Link>
        <p className="text-sm text-slate-500">Last updated: {updatedAt}</p>
        <h1 className="mt-2 text-3xl font-bold tracking-tight">{title}</h1>
        <div className="mt-8 space-y-6 text-sm leading-7 text-slate-700">
          {children}
        </div>
      </div>
    </main>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="space-y-2">
      <h2 className="text-lg font-semibold text-slate-950">{title}</h2>
      {children}
    </section>
  );
}

export function PrivacyPage() {
  return (
    <LegalShell title="Privacy Policy">
      <Section title="Overview">
        <p>
          JPlan is a feasibility-aware daily planning prototype for creating, editing, importing, and exporting daily
          schedules. This policy explains what data JPlan uses and how it handles Google Calendar data.
        </p>
      </Section>

      <Section title="Information JPlan Collects">
        <p>
          JPlan may store your account profile, saved preferences, saved locations, recent locations, daily plans,
          schedule blocks, travel validation results, and Google Calendar import/export links.
        </p>
      </Section>

      <Section title="Google Calendar Data">
        <p>
          If you connect Google Calendar, JPlan requests access to read calendar events and create or update calendar
          events for schedule export. JPlan uses this data only to show external calendar events, let you import selected
          events into JPlan, replace a selected JPlan day from Google Calendar when you confirm it, and export JPlan
          schedule blocks back to Google Calendar when you choose to do so.
        </p>
        <p>
          JPlan does not sell Google Calendar data, use it for advertising, or transfer it to unrelated third parties.
          Calendar data is kept separate from JPlan tasks unless you explicitly import or replace a JPlan schedule.
        </p>
      </Section>

      <Section title="AI And Travel Services">
        <p>
          JPlan may send planning prompts, schedule context, and location/travel context to backend services used for
          scheduling assistance, geocoding, travel-time estimation, and AI replies. These services are used only to
          provide visible JPlan features.
        </p>
      </Section>

      <Section title="Storage And Security">
        <p>
          JPlan stores application data in Supabase and runs its frontend/backend on hosted deployment services. Access
          tokens and backend secrets are not intentionally exposed to the frontend. Google refresh tokens, when stored,
          are used by the backend to perform calendar actions you request.
        </p>
      </Section>

      <Section title="Data Deletion">
        <p>
          You may remove saved plans, locations, and imported schedule data inside JPlan where available. You can revoke
          JPlan&apos;s Google access at any time from your Google Account third-party access settings.
        </p>
      </Section>

      <Section title="Contact">
        <p>
          For questions about this prototype or this policy, contact the JPlan project owner using the email configured
          in the Google OAuth consent screen.
        </p>
      </Section>
    </LegalShell>
  );
}

export function TermsPage() {
  return (
    <LegalShell title="Terms of Service">
      <Section title="Prototype Use">
        <p>
          JPlan is an academic prototype intended to help users create and evaluate daily schedules. It is provided for
          testing and demonstration purposes and may change during development.
        </p>
      </Section>

      <Section title="User Responsibility">
        <p>
          You are responsible for reviewing schedules before relying on them. JPlan may generate imperfect travel times,
          calendar imports, AI suggestions, or schedule placements.
        </p>
      </Section>

      <Section title="Google Calendar Actions">
        <p>
          JPlan will only import selected Google Calendar events into JPlan or export JPlan schedules to Google Calendar
          when you use the relevant controls. Some export actions can update or replace calendar events; review prompts
          carefully before confirming.
        </p>
      </Section>

      <Section title="Acceptable Use">
        <p>
          Do not use JPlan to store unlawful content, misuse Google Calendar access, interfere with the service, or
          attempt to access another user&apos;s data.
        </p>
      </Section>

      <Section title="No Warranty">
        <p>
          JPlan is provided as is. The prototype does not guarantee schedule accuracy, route accuracy, uptime, or fitness
          for a particular purpose.
        </p>
      </Section>

      <Section title="Changes">
        <p>
          These terms may be updated as the prototype changes. Continued use of JPlan after updates means you accept the
          updated terms.
        </p>
      </Section>
    </LegalShell>
  );
}
