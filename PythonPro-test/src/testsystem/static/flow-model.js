"use strict";

/**
 * 流程定义的不可变编辑模型（步骤/动作的插入、移动、删除）。
 *
 * 供流程编排页的列表编辑器与图形画布共用：每个操作都返回深拷贝后的新草稿，
 * 不修改传入对象，调用方用返回值整体替换 state.draft，避免拖拽/编辑产生共享引用。
 * 通过 UMD 同时暴露给浏览器（window.FlowModel）与 Node（module.exports，便于单测）。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FlowModel = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  /** 深拷贝草稿，保证编辑操作不污染原对象。 */
  function copy(value) {
    return JSON.parse(JSON.stringify(value));
  }

  /**
   * 校验下标合法性；allowEnd 为 true 时允许等于 length（用于尾部插入）。
   * 越界抛 RangeError，由调用方提示“无法放置节点”。
   */
  function checkIndex(value, length, allowEnd = false) {
    const maximum = allowEnd ? length : length - 1;
    if (!Number.isInteger(value) || value < 0 || value > maximum) {
      throw new RangeError(`Index ${value} is outside 0..${maximum}`);
    }
  }

  /** 在 index 处插入测试项（缺省追加到末尾），返回新草稿。 */
  function insertStep(draft, step, index) {
    const next = copy(draft);
    next.steps ||= [];
    const target = index ?? next.steps.length;
    checkIndex(target, next.steps.length, true);
    next.steps.splice(target, 0, copy(step));
    return next;
  }

  /**
   * 移动测试项：source 为当前下标，target 为“插入点”下标（允许等于 length 表示移到末尾）。
   * 先删除后插入时，若原位置在插入点之前，实际落点要减 1。
   */
  function moveStep(draft, source, target) {
    const next = copy(draft);
    checkIndex(source, next.steps.length);
    checkIndex(target, next.steps.length, true);
    const [step] = next.steps.splice(source, 1);
    const adjusted = source < target ? target - 1 : target;
    next.steps.splice(adjusted, 0, step);
    return next;
  }

  /** 在指定测试项的 actionIndex 处插入动作（缺省追加），返回新草稿。 */
  function insertAction(draft, stepIndex, action, actionIndex) {
    const next = copy(draft);
    checkIndex(stepIndex, next.steps.length);
    const actions = next.steps[stepIndex].actions ||= [];
    const target = actionIndex ?? actions.length;
    checkIndex(target, actions.length, true);
    actions.splice(target, 0, copy(action));
    return next;
  }

  /** 移动动作（支持跨测试项）；同测试项内后移时落点同样需要做 -1 修正。 */
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

  /** 删除指定测试项，返回新草稿。 */
  function removeStep(draft, index) {
    const next = copy(draft);
    checkIndex(index, next.steps.length);
    next.steps.splice(index, 1);
    return next;
  }

  /** 删除指定测试项中的动作，返回新草稿。 */
  function removeAction(draft, stepIndex, actionIndex) {
    const next = copy(draft);
    checkIndex(stepIndex, next.steps.length);
    checkIndex(actionIndex, next.steps[stepIndex].actions.length);
    next.steps[stepIndex].actions.splice(actionIndex, 1);
    return next;
  }

  return { insertStep, moveStep, insertAction, moveAction, removeStep, removeAction };
});
