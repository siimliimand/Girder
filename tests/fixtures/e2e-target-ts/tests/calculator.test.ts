/** Baseline suite for the TS e2e fixture repo (all green by construction). */
import { Calculator } from "../src/calculator";

describe("Calculator", () => {
  it("adds", () => {
    expect(new Calculator().add(2, 3)).toBe(5);
  });

  it("subtracts", () => {
    expect(new Calculator().subtract(7, 4)).toBe(3);
  });

  it("multiplies", () => {
    expect(new Calculator().multiply(6, 7)).toBe(42);
  });
});
