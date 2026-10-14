import { useConfig } from "../hooks/useConfig";
import { useMode } from "../hooks/useMode";
import { cx } from "../lib/cx";
import { enabledModes, type Mode } from "../lib/mode";

const OPTIONS: { value: Mode; label: string }[] = [
  { value: "business", label: "Business" },
  { value: "personal", label: "Personal" },
];

/**
 * Business | Personal switch. Not the brand switcher: that one picks a
 * business inside Business mode. The choice is kept in the URL and in
 * localStorage (hpgbooks.mode) and the date range carries over. Hidden when
 * the install has only one mode (features.business / features.personal).
 */
export function ModeToggle({ compact }: { compact?: boolean }) {
  const { mode, switchMode } = useMode();
  const modes = enabledModes(useConfig().features);
  if (modes.length < 2) return null;
  return (
    <div className={cx("mode-toggle", compact && "mode-toggle--compact", `mode-toggle--${mode}`)} role="group" aria-label="Mode">
      {OPTIONS.filter((option) => modes.includes(option.value)).map((option) => (
        <button
          key={option.value}
          type="button"
          className={cx("mode-toggle__item", mode === option.value && "is-active")}
          aria-pressed={mode === option.value}
          onClick={() => switchMode(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
