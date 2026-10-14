import { Laya } from "@receptron/laya";
import { readFile } from "node:fs/promises";

let input = "";
for await (const chunk of process.stdin) input += chunk;
const { task } = JSON.parse(input);
const questions = JSON.parse(await readFile(process.env.LAYA_QUESTIONS || new URL("./questions.json", import.meta.url), "utf8"));
const laya = await Laya.load();
const result = await laya.systemOne({ task: task.slice(0, 1500) }, questions);

// Laya 0.1.x nests answers under `answers`; the tier score is 0-based (0 = low).
const answers = result.answers;
const noul = (name) => (Object.hasOwn(questions, name) ? answers[name].noul : null);
console.log(JSON.stringify({
  tool: answers.tool.choice,
  tool_p: answers.tool.probabilities[answers.tool.choice],
  tool_confidence: answers.tool.confidence,
  tool_probabilities: answers.tool.probabilities,
  tier_score: answers.tier.score,
  tier_probabilities: answers.tier.probabilities,
  hijack: noul("hijack"),
  mechanical: noul("mechanical"),
  big: noul("big")
}));
