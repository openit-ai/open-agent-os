/**
 * The control group pages (policy / approvals / audit) were reported as
 * "not localized" — the screens rendered English labels even though the
 * catalog carried keys. These tests lock in that the labels those screens use
 * actually resolve to Korean, and that the catalog values are really translated
 * rather than English strings parked under Korean keys.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { I18nProvider, LANG_STORAGE_KEY, useI18n } from "@/lib/i18n";
import en from "@/lib/i18n/en.json";
import ko from "@/lib/i18n/ko.json";

/** Values that are identifiers, not copy: identical in both languages by design. */
const IDENTIFIER_KEYS = new Set([
  "policy.colSource",
  "policy.colAction",
  "policy.colResource",
  "policy.colDecision",
  "policy.colPriority",
  "policy.colOrder",
  "admin.policy.col.id",
  "admin.policy.col.action",
  "admin.policy.col.resourcePattern",
  "admin.policy.col.effect",
  "admin.policy.draft.allowRemoveMandatory",
  "admin.policy.simulate.action",
  "admin.policy.simulate.resource",
  "admin.policy.simulate.useDraft",
  "audit.chainValid",
  "audit.checkpoint",
  "audit.count",
]);

function getNested(value: unknown, path: string): unknown {
  return path.split(".").reduce<unknown>(
    (acc, part) => (acc && typeof acc === "object" ? (acc as Record<string, unknown>)[part] : undefined),
    value,
  );
}

/** Every key the three control screens were given, read straight off the pages. */
const SCREEN_KEYS = [
  // policy
  "admin.policy.title",
  "admin.policy.tab.published",
  "admin.policy.tab.draft",
  "admin.policy.tab.simulate",
  "admin.policy.tab.history",
  "admin.policy.active.publishedVersion",
  "admin.policy.active.noDraft",
  "admin.policy.published.missing",
  "admin.policy.draft.title",
  "admin.policy.draft.description",
  "admin.policy.draft.bundleId",
  "admin.policy.draft.name",
  "admin.policy.draft.rulesTable",
  "admin.policy.draft.addRule",
  "admin.policy.draft.noRules",
  "admin.policy.draft.rawJson",
  "admin.policy.draft.loadJson",
  "admin.policy.draft.tableToJson",
  "admin.policy.draft.loadedFromJson",
  "admin.policy.draft.editDraft",
  "admin.policy.draft.saving",
  "admin.policy.draft.saveDraft",
  "admin.policy.draft.validate",
  "admin.policy.draft.approve",
  "admin.policy.draft.publish",
  "admin.policy.draft.validationPassed",
  "admin.policy.draft.validationFailed",
  "admin.policy.draft.invalidJson",
  "admin.policy.draft.emptyRules",
  "admin.policy.draft.saved",
  "admin.policy.draft.approved",
  "admin.policy.draft.published",
  "admin.policy.draft.publishFailed",
  "admin.policy.draft.rolledBack",
  "admin.policy.draft.rollbackFailed",
  "admin.policy.simulate.title",
  "admin.policy.simulate.description",
  "admin.policy.simulate.run",
  "admin.policy.simulate.decision",
  "admin.policy.simulate.source",
  "admin.policy.history.title",
  "admin.policy.history.description",
  "admin.policy.history.activeBadge",
  "admin.policy.history.emptyTitle",
  "admin.policy.history.emptyDescription",
  "admin.policy.history.ariaLabel",
  "admin.policy.col.version",
  "admin.policy.col.status",
  "admin.policy.col.bundle",
  "admin.policy.col.rules",
  "admin.policy.col.createdBy",
  "admin.policy.col.createdAt",
  "admin.policy.action.rollback",
  "admin.policy.confirm.publishTitle",
  "admin.policy.confirm.rollbackTitle",
  "admin.policy.confirm.publishDescription",
  "admin.policy.confirm.rollbackDescription",
  "admin.policy.confirm.publishConfirm",
  "admin.policy.confirm.rollbackConfirm",
  "admin.policy.source.explicit_deny",
  "admin.policy.source.default_deny",
  // approvals
  "approvals.title",
  "approvals.colRequester",
  "approvals.colAgent",
  "approvals.colAction",
  "approvals.colResource",
  "approvals.colRisk",
  "approvals.ariaList",
  "approvals.decideOnce",
  "approvals.decideUserAlways",
  "approvals.decideGroupAlways",
  "approvals.decideDeny",
  // audit
  "audit.title",
  "audit.colEvent",
  "audit.colActor",
  "audit.colAction",
  "audit.colResource",
  "audit.colDecision",
  "audit.ariaTimeline",
];

describe("control group localization", () => {
  it("resolves every control-screen key to a Korean value", () => {
    const missing = SCREEN_KEYS.filter((k) => typeof getNested(ko, k) !== "string");
    expect(missing).toEqual([]);
  });

  it("translates the control-screen labels instead of parking English under Korean keys", () => {
    const untranslated = SCREEN_KEYS.filter((k) => {
      if (IDENTIFIER_KEYS.has(k)) return false;
      const koValue = getNested(ko, k);
      const enValue = getNested(en, k);
      return typeof koValue === "string" && koValue === enValue;
    });
    expect(untranslated).toEqual([]);
  });

  it("keeps the English and Korean key sets identical", () => {
    const leaves = (value: unknown, prefix = ""): string[] =>
      value == null || typeof value !== "object" || Array.isArray(value)
        ? [prefix]
        : Object.entries(value).flatMap(([k, v]) => leaves(v, prefix ? `${prefix}.${k}` : k));
    expect(leaves(en).sort()).toEqual(leaves(ko).sort());
  });

  it("renders Korean copy when the saved language is Korean", async () => {
    localStorage.setItem(LANG_STORAGE_KEY, "ko");

    function Probe() {
      const { t } = useI18n();
      return (
        <>
          <span data-testid="policy-title">{t("admin.policy.title")}</span>
          <span data-testid="policy-tab">{t("admin.policy.tab.published")}</span>
          <span data-testid="policy-empty">{t("admin.policy.draft.noRules")}</span>
          <span data-testid="approvals-head">{t("approvals.colRequester")}</span>
          <span data-testid="audit-head">{t("audit.colDecision")}</span>
          <span data-testid="policy-source">{t("admin.policy.source.explicit_deny")}</span>
        </>
      );
    }

    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>,
    );

    await waitFor(() => expect(screen.getByTestId("policy-title")).toHaveTextContent("정책 번들"));
    expect(screen.getByTestId("policy-tab")).toHaveTextContent("게시됨");
    expect(screen.getByTestId("policy-empty")).toHaveTextContent("규칙이 없습니다");
    expect(screen.getByTestId("approvals-head")).toHaveTextContent("요청자");
    expect(screen.getByTestId("audit-head")).toHaveTextContent("판정");
    expect(screen.getByTestId("policy-source")).toHaveTextContent("명시적 거부");
  });

  it("renders English copy when the saved language is English", async () => {
    localStorage.setItem(LANG_STORAGE_KEY, "en");

    function Probe() {
      const { t } = useI18n();
      return <span data-testid="policy-title">{t("admin.policy.title")}</span>;
    }

    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>,
    );

    await waitFor(() => expect(screen.getByTestId("policy-title")).toHaveTextContent("Policy Bundles"));
  });
});
