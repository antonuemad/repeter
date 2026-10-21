# Schedule and rotation usability — 2026-10-21

## Customer behavior

The person's Times section now shows start date and zone before weekday selection. A native
Every day / specific days choice removes unnecessary checkboxes in the daily case. One main time
is required; up to four additional distinct times remain optional. A local summary shows weekly
requested slots **per page**, rather than promising that the library can fill every slot.

Once-per-page and repeat modes have adjacent descriptions. Once mode hides and disables its
inactive repeat field without losing the saved interval. With JavaScript unavailable, the same
native form works: daily mode explicitly takes precedence over weekday checkboxes. Library order
is the remaining advanced choice. Validation identifies dates, zone, days or times, retains the
draft and rolls back the complete schedule if any field fails.

Preview states its ordinary 30-day schedule window and zone, and explains that later simulated
choices assume earlier success. Manually moved slots can extend beyond the ordinary window.
Full-result gap counts precede filtering/pagination. Times with gaps retains filled destinations
in partial rows and preserves the filter when paging. A zero-slot result cannot claim full coverage.

Recovery distinguishes interval waits, once-mode exhaustion (including actual history), pending
or uncertain results, disconnected/revoked/reassigned pages, slot exclusions, unavailable replacements,
preparing/failed content, an empty ready library, incompatible formats, origin-account avoidance and
platform exclusions. Mixed eligibility causes direct the user to review the library rather than
inventing one cause. Earliest eligibility is not a reservation. Links stay within the owned person's
Pages, Content or Times, or go to activity/slot editing as appropriate. Safety holds remain intact.

Filtered Calendar uses the person's plan zone, including local date boundaries. Unselected days,
dates before start or outside the current window, passed times, missing selected pages and nonexistent daylight-saving times
have explicit explanations. Per-slot recovery detail is expandable to keep the weekly view readable.
All-people Calendar retains workspace time. Owned people beyond the dropdown limit remain accessible.

## Verification

65 new mocked acceptance cases cover native daily/custom submission, optional once interval,
atomic AR/EN validation, every major gap category, full-result totals, partial rows, filtered
pagination, zero-slot states, read-only simulation, selected-plan time zones, DST day explanations,
CSRF/stale revisions and tenant boundaries. Existing rotation tests cover 24-hour cooldowns across
DST and pending-result holds. Application regression tests include setup, mixed media, imports,
security, person home, calendar, guide and capacity behavior.

Browser acceptance uses only the local synthetic fixture (no worker; both publishing flags false;
provider HTTP denied): AR/EN, light/dark, desktop and 390px mobile; native custom/daily selection,
once-mode hiding, live demand, save/preview, filtered pagination and Calendar reasons. Fixed two
rendered issues: flex layout overriding hidden weekdays and narrow mobile preview headings. New
filter/recovery actions have at least 44px touch height. User guide matches the updated controls.

Initial local deployment-shell tests fail under macOS Bash 3 (`declare -A` unsupported); the
unmodified deployment scripts require modern Bash/Linux. Full GitHub CI, including Docker,
media/import checks and disposable backup/restore tests, is required green on the exact release SHA.

## Safety and scope

No new migrations, provider scopes, posting calls, media containers or draft uploads. No production
person, library or account is used as a mutable test fixture. Production publishing and TikTok
publication auditing remain false; plans/rules remain paused. Release checks require the exact
CI SHA, matching runtime file hashes in web and worker, unchanged protected environment, public
asset checks and read-only before/after audits showing no scheduled/published posts or posting jobs.

The incumbent Magic Lap / Sentinel Eye typography and identity remain intentional. No new design
suppression or font substitution was added for this work.
