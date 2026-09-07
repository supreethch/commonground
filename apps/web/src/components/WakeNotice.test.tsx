import { render, screen, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WakeNotice, useElapsedWhile } from "./WakeNotice";

/* The threshold is the whole point of this component, so it is what gets
 * tested: firing early would announce a cold start on every ordinary sign-in,
 * and never firing would leave the blank-looking wait it exists to explain.
 */

function Probe({ active }: { active: boolean }) {
  return <WakeNotice seconds={useElapsedWhile(active)} />;
}

describe("useElapsedWhile", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("stays silent through a normal-length request", () => {
    render(<Probe active />);
    act(() => void vi.advanceTimersByTime(2500));
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("explains the wait once the request is clearly slow, and counts up", () => {
    render(<Probe active />);
    act(() => void vi.advanceTimersByTime(3000));
    expect(screen.getByRole("status")).toHaveTextContent("Starting the server.");

    act(() => void vi.advanceTimersByTime(4000));
    expect(screen.getByRole("status")).toHaveTextContent("7s");
  });

  it("disappears when the request finishes", () => {
    const { rerender } = render(<Probe active />);
    act(() => void vi.advanceTimersByTime(4000));
    expect(screen.getByRole("status")).toBeInTheDocument();

    rerender(<Probe active={false} />);
    expect(screen.queryByRole("status")).toBeNull();
  });
});
