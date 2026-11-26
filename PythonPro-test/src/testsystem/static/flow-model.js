"use strict";

(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FlowModel = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  function copy(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function checkIndex(value, length, allowEnd = false) {
    const maximum = allowEnd ? length : length - 1;
    if (!Number.isInteger(value) || value < 0 || value > maximum) {
      throw new RangeError(`Index ${value} is outside 0..${maximum}`);
    }
  }

  function insertStep(draft, step, index) {
    const next = copy(draft);
    next.steps ||= [];
    const target = index ?? next.steps.length;
    checkIndex(target, next.steps.length, true);
    next.steps.splice(target, 0, copy(step));
    return next;
  }

  function moveStep(draft, source, target) {
    const next = copy(draft);
    checkIndex(source, next.steps.length);
    checkIndex(target, next.steps.length, true);
    const [step] = next.steps.splice(source, 1);
    const adjusted = source < target ? target - 1 : target;
    next.steps.splice(adjusted, 0, step);
    return next;
  }

  function insertAction(draft, stepIndex, action, actionIndex) {
    const next = copy(draft);
    checkIndex(stepIndex, next.steps.length);
    const actions = next.steps[stepIndex].actions ||= [];
    const target = actionIndex ?? actions.length;
    checkIndex(target, actions.length, true);
    actions.splice(target, 0, copy(action));
    return next;
  }

  function moveAction(draft, sourceStep, sourceAction, targetStep, targetAction) {
    const next = copy(draft);
    checkIndex(sourceStep, next.steps.length);
    checkIndex(targetStep, next.steps.length);
    const sourceActions = next.steps[sourceStep].actions;
    checkIndex(sourceAction, sourceActions.length);
    const targetLength = next.steps[targetStep].actions.length;
    checkIndex(targetAction, targetLength, true);
    const [action] = sourceActions.splice(sourceAction, 1);
    const adjusted = sourceStep === targetStep && sourceAction < targetAction ? targetAction - 1 : targetAction;
    next.steps[targetStep].actions.splice(adjusted, 0, action);
    return next;
  }

  function removeStep(draft, index) {
    const next = copy(draft);
    checkIndex(index, next.steps.length);
    next.steps.splice(index, 1);
    return next;
  }

  function removeAction(draft, stepIndex, actionIndex) {
    const next = copy(draft);
    checkIndex(stepIndex, next.steps.length);
    checkIndex(actionIndex, next.steps[stepIndex].actions.length);
    next.steps[stepIndex].actions.splice(actionIndex, 1);
    return next;
  }

  return { insertStep, moveStep, insertAction, moveAction, removeStep, removeAction };
});
