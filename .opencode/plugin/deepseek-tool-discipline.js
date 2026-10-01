// Tool-discipline nudges for the local DeepSeek and Qwen web-chat bridge.
//
// The bridge emulates function calling via prompt injection: these web-chat
// protocols have no OpenAI tool channel, so models reply with a ```tool_calls JSON
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

const PROVIDER_IDS = new Set(["local-deepseek", "local-qwen"]);

export const DeepSeekToolDiscipline = async () => ({
  "experimental.chat.system.transform": async (input, output) => {
    if (!PROVIDER_IDS.has(input?.model?.providerID)) return;
    output.system.push(
      [
        "CRITICAL — tool discipline for the local web-chat bridge:",
        "To perform an action, your ENTIRE reply must be exactly one fenced",
        "```tool_calls JSON array in the format specified by the bridge.",
        "Do not put prose, examples, XML, DSML, or pseudo-calls around that block.",
        "The bridge converts only this complete block into API tool calls.",
        "If a file must be created, written, or edited, invoke the corresponding",
        "tool immediately. If a shell command must run, call the bash tool immediately.",
        "Only answer in plain text when no tool is needed.",
      ].join(" ")
    );
  },
});

export default DeepSeekToolDiscipline;
