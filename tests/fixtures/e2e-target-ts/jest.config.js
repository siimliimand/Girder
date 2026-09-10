/** jest.config.js — ts-jest + jest-junit (report path from the environment). */
module.exports = {
  preset: "ts-jest",
  testEnvironment: "node",
  roots: ["<rootDir>/tests"],
  reporters: ["default", "jest-junit"],
};
