<?php
// A small guestbook: forms with a CSRF cookie, redirects after a post, HTML pages, entries in a JSON file.
// The reference implementation; js/, ts/ and rust/ are ports parityrun checks against it.
//
//   php -S 127.0.0.1:8080 index.php        (GUESTBOOK_DATA: the data folder)

const NAME_MAX = 40;
const MESSAGE_MAX = 200;

$data = getenv('GUESTBOOK_DATA') ?: __DIR__ . '/data';
$file = $data . '/entries.json';

function e(string $s): string
{
    return htmlspecialchars($s, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}

function chars(string $s): int
{
    return preg_match_all('/./su', $s);
}

function field(string $name): string
{
    $value = $_POST[$name] ?? '';
    return is_string($value) ? $value : '';
}

function load_entries(string $file): array
{
    return is_file($file) ? json_decode(file_get_contents($file), true) : [];
}

function save_entries(string $data, string $file, array $entries): void
{
    if (!is_dir($data)) {
        mkdir($data, 0777, true);
    }
    file_put_contents($file, json_encode($entries, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE));
}

function token(): string
{
    if (isset($_COOKIE['csrf']) && is_string($_COOKIE['csrf']) && $_COOKIE['csrf'] !== '') {
        return $_COOKIE['csrf'];
    }
    static $made = null;
    if ($made === null) {
        $made = bin2hex(random_bytes(16));
        setcookie('csrf', $made, ['path' => '/', 'httponly' => true, 'samesite' => 'Lax']);
    }
    return $made;
}

function token_ok(): bool
{
    $sent = field('csrf');
    return isset($_COOKIE['csrf']) && is_string($_COOKIE['csrf']) && $sent !== '' && hash_equals($_COOKIE['csrf'], $sent);
}

function page(int $status, string $title, string $body): void
{
    http_response_code($status);
    header('Content-Type: text/html; charset=utf-8');
    echo "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"utf-8\">\n";
    echo '  <title>' . e($title) . " · Guestbook</title>\n</head>\n<body>\n";
    echo "  <h1><a href=\"/\">Guestbook</a></h1>\n" . $body . "</body>\n</html>\n";
}

function redirect(string $to): void
{
    http_response_code(303);
    header('Location: ' . $to);
}

function not_found(): void
{
    page(404, 'Not found', "  <p>Page not found.</p>\n");
}

function index(array $entries, array $errors = [], string $name = '', string $message = '', int $status = 200): void
{
    $body = "  <form method=\"post\" action=\"/sign\">\n";
    $body .= '    <input type="hidden" name="csrf" value="' . e(token()) . "\">\n";
    if ($errors) {
        $body .= "    <ul class=\"errors\">\n";
        foreach ($errors as $error) {
            $body .= '      <li>' . e($error) . "</li>\n";
        }
        $body .= "    </ul>\n";
    }
    $body .= '    <label>Name <input name="name" maxlength="' . NAME_MAX . '" value="' . e($name) . "\"></label>\n";
    $body .= '    <label>Message <textarea name="message">' . e($message) . "</textarea></label>\n";
    $body .= "    <button>Sign</button>\n  </form>\n";
    $count = count($entries);
    $body .= '  <h2>' . $count . ($count === 1 ? ' entry' : ' entries') . "</h2>\n";
    if (!$entries) {
        $body .= "  <p class=\"empty\">No entries yet. Be the first!</p>\n";
    } else {
        $body .= "  <ul class=\"entries\">\n";
        foreach (array_reverse($entries) as $entry) {
            $body .= '    <li><a href="/entries/' . e($entry['id']) . '">' . e($entry['name']) . '</a>: '
                . e($entry['message']) . "</li>\n";
        }
        $body .= "  </ul>\n";
    }
    page($status, 'Sign the guestbook', $body);
}

function show(array $entry): void
{
    $when = gmdate('Y-m-d H:i', strtotime($entry['at']));
    $body = "  <article>\n    <h2>" . e($entry['name']) . "</h2>\n";
    $body .= '    <p>' . nl2br(e($entry['message'])) . "</p>\n";
    $body .= '    <p class="meta">Signed ' . $when . " UTC</p>\n";
    $body .= '    <form method="post" action="/entries/' . e($entry['id']) . "/delete\">\n";
    $body .= '      <input type="hidden" name="csrf" value="' . e(token()) . "\">\n";
    $body .= "      <button>Delete</button>\n    </form>\n  </article>\n";
    page(200, $entry['name'], $body);
}

$method = $_SERVER['REQUEST_METHOD'];
$path = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);
$entries = load_entries($file);

if ($method === 'GET' && $path === '/health') {
    header('Content-Type: text/plain; charset=utf-8');
    echo 'ok';
} elseif ($method === 'GET' && $path === '/') {
    index($entries);
} elseif ($method === 'POST' && $path === '/sign') {
    if (!token_ok()) {
        page(403, 'Expired', "  <p>Your session expired. Reload the page and try again.</p>\n");
        return;
    }
    $name = trim(field('name'));
    $message = trim(field('message'));
    $errors = [];
    if ($name === '') {
        $errors[] = 'Please write your name.';
    } elseif (chars($name) > NAME_MAX) {
        $errors[] = 'Your name can be at most ' . NAME_MAX . ' characters.';
    }
    if ($message === '') {
        $errors[] = 'Please write a message.';
    } elseif (chars($message) > MESSAGE_MAX) {
        $errors[] = 'Your message can be at most ' . MESSAGE_MAX . ' characters.';
    }
    if ($errors) {
        index($entries, $errors, $name, $message, 422);
        return;
    }
    $entry = ['id' => bin2hex(random_bytes(4)), 'name' => $name, 'message' => $message, 'at' => gmdate('c')];
    $entries[] = $entry;
    save_entries($data, $file, $entries);
    redirect('/entries/' . $entry['id']);
} elseif (preg_match('#^/entries/([0-9a-f]+)$#', $path, $m) && $method === 'GET') {
    $found = array_values(array_filter($entries, fn ($x) => $x['id'] === $m[1]));
    $found ? show($found[0]) : not_found();
} elseif (preg_match('#^/entries/([0-9a-f]+)/delete$#', $path, $m) && $method === 'POST') {
    if (!token_ok()) {
        page(403, 'Expired', "  <p>Your session expired. Reload the page and try again.</p>\n");
        return;
    }
    $left = array_values(array_filter($entries, fn ($x) => $x['id'] !== $m[1]));
    if (count($left) === count($entries)) {
        not_found();
        return;
    }
    save_entries($data, $file, $left);
    redirect('/');
} else {
    not_found();
}
