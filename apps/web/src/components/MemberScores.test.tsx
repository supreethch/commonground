import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { MemberScores } from "./MemberScores";

/* The satisfaction strip is the one component carrying a claim rather than a
 * layout, so it is the one worth testing: if it marks the wrong member as worst
 * served, the interface is lying about the thing the product is for.
 */

const MEMBERS = [
  { id: "a", name: "Alex", score: 0.87 },
  { id: "b", name: "Rio", score: 0.42 },
  { id: "c", name: "Sam", score: 0.96 },
];

describe("MemberScores", () => {
  it("names the least-satisfied member, not the first or the last", async () => {
    render(<MemberScores members={MEMBERS} />);
    await userEvent.click(screen.getByRole("button"));

    // Scoped to the summary line: "Rio" also appears in the member list above
    // it, so an unscoped getByText finds two nodes and fails for the wrong
    // reason.
    const summary = screen.getByText(/is served least by this track/);
    expect(summary).toHaveTextContent("Rio is served least by this track.");
  });

  it("shows every member's score", async () => {
    render(<MemberScores members={MEMBERS} />);
    await userEvent.click(screen.getByRole("button"));

    expect(screen.getByText("0.87")).toBeInTheDocument();
    expect(screen.getByText("0.42")).toBeInTheDocument();
    expect(screen.getByText("0.96")).toBeInTheDocument();
  });

  it("does not single anyone out in a room of one", async () => {
    render(<MemberScores members={[{ id: "a", name: "Alex", score: 0.5 }]} />);
    await userEvent.click(screen.getByRole("button"));

    // "Alex is served least" is meaningless when Alex is the only member.
    expect(screen.queryByText(/is served least/)).not.toBeInTheDocument();
  });

  it("renders one bar per member and toggles the detail panel", async () => {
    const { container } = render(<MemberScores members={MEMBERS} />);
    const toggle = screen.getByRole("button");

    expect(container.querySelectorAll("span[style*='height']")).toHaveLength(3);
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    await userEvent.click(toggle);
    expect(screen.queryByText(/is served least/)).not.toBeInTheDocument();
  });

  it("keeps a zero-score member visible", async () => {
    // A bar that vanishes reads as "no data", which is a different fact from
    // "served badly" -- and the second is the one worth seeing.
    const { container } = render(
      <MemberScores members={[...MEMBERS, { id: "d", name: "Theo", score: 0 }]} />,
    );
    const bars = container.querySelectorAll<HTMLElement>("span[style*='height']");
    const last = bars[bars.length - 1];

    expect(last).toBeDefined();
    expect(Number.parseInt(last!.style.height, 10)).toBeGreaterThan(0);
  });

  it("renders nothing when there are no members", () => {
    const { container } = render(<MemberScores members={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
