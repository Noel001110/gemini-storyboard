const fs = require('fs');
const html = fs.readFileSync('dashboard.html', 'utf8');
const js = html.match(/<script>([\s\S]*?)<\/script>/)[1];
try {
  new Function(js);
  console.log("Syntax OK");
} catch(e) {
  console.log("Syntax ERROR: ", e);
}
