// Tool-discipline nudges for the local DeepSeek bridge (provider id "local-deepseek").
//
// The bridge emulates function calling via prompt injection: DeepSeek Web has no
// tool-calling channel, so it is asked to reply with a fenced ```tool_calls JSON
// block. Models often prefer to *describe* an action or leak native markup
// instead. This plugin appends an authoritative, late system instruction so the
// model actually emits tool calls and opencode can execute edits/commands.
//
// Install (auto-discovered, no config entry needed):
//   * project scope : .opencode/plugin/deepseek-tool-discipline.js   (this file)
//   * global scope  : ~/.config/opencode/plugin/deepseek-tool-discipline.js
//
// Hooks are `experimental.*` (opencode ~1.18.x). If a future opencode renames
// them, the plugin simply stops firing — it never breaks startup.

const PROVIDER_ID = "local-deepseek";

export const DeepSeekToolDiscipline = async () => ({
  "experimental.chat.system.transform": async (input, output) => {
    if (input?.model?.providerID !== PROVIDER_ID) return;
    output.system.push(
      [
        "CRITICAL — tool discipline for the DeepSeek bridge:",
        "To perform ANY action you MUST emit real tool calls through the API's",
        "tool-calling channel. Do not describe, plan, or explain the action you",
        "are about to take.",
        "Never print JSON, XML, DSML, or a ```tool_calls block as visible text —",
        "that does not execute anything.",
        "If a file must be created, written, or edited, invoke the corresponding",
        "tool immediately. If a shell command must run, call the bash tool immediately.",
        "Only answer in plain text when no tool is needed.",
      ].join(" ")
    );
  },
});

export default DeepSeekToolDiscipline;
