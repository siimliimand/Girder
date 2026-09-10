/**
 * A tiny calculator the Girder agent loop can genuinely extend.
 *
 * `divide` is deliberately ABSENT: the SC-20 e2e scenario asks the
 * (scripted) agent to implement it against this file.
 */
export class Calculator {
  /** Return the sum of `a` and `b`. */
  add(a: number, b: number): number {
    return a + b;
  }

  /** Return `a` minus `b`. */
  subtract(a: number, b: number): number {
    return a - b;
  }

  /** Return the product of `a` and `b`. */
  multiply(a: number, b: number): number {
    return a * b;
  }
}
