<?php
// parityrun's library shim: a PHP library's functions and classes served over HTTP, one call a request, for the
// `fn` and `new` steps of scenarios. Objects live on between calls under the names steps give them (`as`).
//
//   php library.php       PORT: where to listen (127.0.0.1); PARITYRUN_LIB: the files to load, comma-separated,
//                         relative to the working folder, wildcards allowed (default: vendor/autoload.php)
//
//   GET  /health          200 "ok"
//   POST /call            {"fn": "name" | "Class::method" | "object.method", "args": [...] or {...}, "as": "object"}
//                         {"new": "Class", "args": [...], "as": "object"}
//                         -> {"value": ...} or {"error": {"class": ..., "message": ...}}, and "output" when the
//                         call printed something. A value that is an object is {"$object": class, its public
//                         properties}; an enum case {"$enum": "Class::CASE"}.

ini_set('display_errors', '0');

$lib = getenv('PARITYRUN_LIB');
$patterns = $lib !== false && $lib !== '' ? explode(',', $lib) : (is_file('vendor/autoload.php') ? ['vendor/autoload.php'] : []);
foreach ($patterns as $pattern) {
    $files = glob(trim($pattern)) ?: [trim($pattern)];
    sort($files);
    foreach ($files as $file) {
        require_once $file;
    }
}

function parityrun_data($value)
{
    if (is_array($value)) {
        return array_map('parityrun_data', $value);
    }
    if (is_float($value) && !is_finite($value)) {
        return is_nan($value) ? 'NAN' : ($value > 0 ? 'INF' : '-INF');
    }
    if ($value instanceof \UnitEnum) {
        return ['$enum' => get_class($value) . '::' . $value->name];
    }
    if ($value instanceof \JsonSerializable) {
        return ['$object' => get_class($value), 'json' => parityrun_data($value->jsonSerialize())];
    }
    if (is_object($value)) {
        return ['$object' => get_class($value)] + parityrun_data(get_object_vars($value));
    }
    if (is_resource($value)) {
        return ['$resource' => get_resource_type($value)];
    }
    return $value;
}

function parityrun_call(array $call, array &$objects): array
{
    $args = $call['args'] ?? [];
    $fn = $call['fn'] ?? '';
    if (isset($call['new'])) {
        $target = static fn () => new $call['new'](...$args);
    } elseif (preg_match('/^([A-Za-z_]\w*)\.(\w+)$/', $fn, $m)) {
        if (!array_key_exists($m[1], $objects)) {
            return [400, ['error' => "no object called {$m[1]}: give a step `as: {$m[1]}` first"]];
        }
        $target = static fn () => $objects[$m[1]]->{$m[2]}(...$args);
    } elseif (str_contains($fn, '::')) {
        [$class, $method] = explode('::', $fn, 2);
        $target = static fn () => $class::$method(...$args);
    } elseif ($fn !== '') {
        $target = static fn () => $fn(...$args);
    } else {
        return [400, ['error' => 'a call needs fn or new']];
    }
    ob_start();
    try {
        $value = $target();
        $answer = ['value' => parityrun_data($value)];
        if (isset($call['as'])) {
            $objects[$call['as']] = $value;
        }
    } catch (\Throwable $e) {
        // a TypeError or ArgumentCountError names the shim's line it was called from: not the library's behaviour
        $message = preg_replace('/,? (?:called )?in ' . preg_quote(__FILE__, '/') . ' on line \d+/', '',
                                $e->getMessage());
        $answer = ['error' => ['class' => get_class($e), 'message' => $message]];
    }
    $output = ob_get_clean();
    if ($output !== '' && $output !== false) {
        $answer['output'] = $output;
    }
    return [200, $answer];
}

$server = stream_socket_server('tcp://127.0.0.1:' . getenv('PORT'), $errno, $errstr);
if (!$server) {
    fwrite(STDERR, "parityrun library shim: can't listen: $errstr\n");
    exit(1);
}
$objects = [];
$reasons = [200 => 'OK', 400 => 'Bad Request', 404 => 'Not Found'];
while (true) {
    $conn = @stream_socket_accept($server, 3600);
    if (!$conn) {
        continue;
    }
    $head = '';
    while (($line = fgets($conn)) !== false && rtrim($line, "\r\n") !== '') {
        $head .= $line;
    }
    preg_match('/^(\S+) (\S+)/', $head, $request);
    $length = preg_match('/^content-length:\s*(\d+)/mi', $head, $l) ? (int) $l[1] : 0;
    $body = '';
    while (strlen($body) < $length && !feof($conn)) {
        $body .= fread($conn, $length - strlen($body));
    }
    $method = $request[1] ?? '';
    $path = $request[2] ?? '';
    if ($method === 'GET' && $path === '/health') {
        [$status, $answer] = [200, 'ok'];
    } elseif ($method === 'POST' && $path === '/call') {
        $call = json_decode($body, true);
        [$status, $answer] = is_array($call) ? parityrun_call($call, $objects) : [400, ['error' => 'not JSON']];
    } else {
        [$status, $answer] = [404, ['error' => "no $method $path: POST /call"]];
    }
    if (isset($GLOBALS['parityrun_coverage_dump'])) {  // coverage=True: the lines run so far, written before
        ($GLOBALS['parityrun_coverage_dump'])();          // the answer, so the run never stops halfway through it
    }
    $json = json_encode($answer, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_PRESERVE_ZERO_FRACTION
                                 | JSON_INVALID_UTF8_SUBSTITUTE | JSON_PARTIAL_OUTPUT_ON_ERROR);
    fwrite($conn, "HTTP/1.1 $status {$reasons[$status]}\r\nContent-Type: application/json\r\n"
                  . 'Content-Length: ' . strlen($json) . "\r\nConnection: close\r\n\r\n" . $json);
    fclose($conn);
}
