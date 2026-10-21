"use strict";

// Independently implemented against markdownlint's public micromark token API.
// A codeFenced block has one opening codeFencedFence and, only when explicitly
// closed in the same Markdown container, a second codeFencedFence. Let the
// parser handle indentation, lists, blockquotes, fence length and fence type.
module.exports = {
  names: ["closed-fences"],
  description: "Fenced code blocks must have an explicit closing fence",
  tags: ["code"],
  parser: "micromark",
  function(params, onError) {
    function visit(tokens) {
      for (const token of tokens) {
        if (token.type === "codeFenced") {
          const fences = token.children.filter((child) => child.type === "codeFencedFence");
          if (fences.length !== 2) {
            onError({
              lineNumber: token.startLine,
              detail: "Close the fence in the same list or blockquote container with matching markers.",
            });
          }
        } else {
          visit(token.children);
        }
      }
    }
    visit(params.parsers.micromark.tokens);
  },
};
