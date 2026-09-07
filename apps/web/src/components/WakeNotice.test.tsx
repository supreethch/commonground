import { render, screen, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FitNotice, WakeNotice, useElapsedWhile } from "./WakeNotice";

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

describe("FitNotice", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("explains the model fit rather than a server start", () => {
    function Probe() {
      return <FitNotice seconds={useElapsedWhile(true)} />;
    }
    render(<Probe />);
    act(() => void vi.advanceTimersByTime(4000));

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Fitting the recommendation model.");
    // The two waits have different causes, so they must not share wording --
    // telling someone the server is starting while it is fitting is a lie.
    expect(status).not.toHaveTextContent("Starting the server");
  });
});
