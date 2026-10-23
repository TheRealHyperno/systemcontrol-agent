const fs = require("fs");

const manifest = {
  name: "hello_js",
  description: "Tiny example JavaScript integration (experimental).",
  prompt: "Use hello_js to greet someone from JavaScript.",
  tools: [
    {
      name: "hello_js",
      description: "Return a greeting from JavaScript.",
      input_schema: {
        type: "object",
        properties: { name: { type: "string", description: "Who to greet" } },
        required: ["name"]
      }
    }
  ]
};

function send(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

const mode = process.argv[2];

if (mode === "manifest") {
  send(manifest);
  process.exit(0);
}

if (mode === "run") {
  let data = "";
  process.stdin.on("data", c => (data += c));
  process.stdin.on("end", () => {
    try {
      const env = JSON.parse(data || "{}");
      const tool = env.tool;
      const args = env.args || {};
      if (tool === "hello_js") {
        send({ greeting: `Hello, ${args.name || "world"}! (from JS)`, author: (env.context || {}).author_name });
      } else {
        send({ error: `unknown tool: ${tool}` });
      }
    } catch (e) {
      send({ error: String(e) });
    }
    process.exit(0);
  });
  return;
}

// no mode
send({ error: "usage: node hello_js.js [manifest|run]" });
process.exit(1);
