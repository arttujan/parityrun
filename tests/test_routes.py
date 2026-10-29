"""Routes read from an app's code, per framework, and requests matched to them."""

import json
import pathlib

import pytest

from parityrun import routes

EXAMPLE = pathlib.Path(__file__).resolve().parents[1] / "examples" / "guestbook"
GUESTBOOK = ["GET /", "GET /entries/{param}", "POST /entries/{param}/delete", "GET /health", "POST /sign"]


def _app(root: pathlib.Path, files: dict) -> pathlib.Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def _keys(root: pathlib.Path) -> list[str]:
    found, _ = routes.scan(root, use_framework=False)
    return [r.key for r in found]


@pytest.mark.parametrize(("raw", "want"), [
    ("/users/:id", "/users/{id}"), ("/posts/<int:pk>/", "/posts/{pk}"), ("posts/{post?}", "/posts/{post}"),
    ("/n/{id:\\d+}", "/n/{id}"), ("/files/{*rest}", "/files/{rest}"), ("", "/"), ("/", "/"), ("/a/<slug>", "/a/{slug}"),
])
def test_every_parameter_notation_becomes_braces(raw, want):
    assert routes.template(raw) == want


def test_a_path_regex_becomes_a_route():
    assert routes.from_regex("^/entries/([0-9a-f]+)/delete$") == "/entries/{param}/delete"
    assert routes.from_regex("^\\/posts\\/(?P<year>\\d{4})\\/(?<slug>[\\w-]+)$") == "/posts/{year}/{slug}"
    assert routes.from_regex("^/a\\.html$") == "/a.html"
    assert routes.from_regex("^/(a|b)+x[0-9]$") is None  # not a plain path: left out rather than guessed


@pytest.mark.parametrize("folder", ["php", "js", "ts"])
def test_hand_made_routers_in_the_guestbook_are_found(folder):
    assert _keys(EXAMPLE / folder) == GUESTBOOK


def test_laravel_route_files_with_groups_resources_and_match(tmp_path):
    app = _app(tmp_path, {"artisan": "", "routes/web.php": """<?php
Route::get('/', [HomeController::class, 'index']);
Route::view('/about', 'about');
Route::match(['get', 'post'], '/contact', ContactController::class);
Route::prefix('admin')->group(function () {
    Route::get('/users/{user}', [UserController::class, 'show']);
    Route::group(['prefix' => 'reports'], function () {
        Route::delete('{report?}', [ReportController::class, 'destroy']);
    });
});
Route::post('/logout', LogoutController::class);
Route::resource('photos', PhotoController::class);
""", "public/index.php": "<?php require '../vendor/autoload.php';"})
    assert _keys(app) == [
        "GET /", "GET /about", "DELETE /admin/reports/{report}", "GET /admin/users/{user}", "GET /contact",
        "POST /contact", "POST /logout", "GET /photos", "POST /photos", "GET /photos/create", "GET /photos/{photo}",
        "PUT /photos/{photo}", "PATCH /photos/{photo}", "DELETE /photos/{photo}", "GET /photos/{photo}/edit"]
    found, _ = routes.scan(app, use_framework=False)
    assert next(r for r in found if r.key == "GET /admin/users/{user}").source == "routes/web.php:6"


def test_symfony_attributes_with_a_class_prefix(tmp_path):
    app = _app(tmp_path, {"bin/console": "", "src/Controller/BlogController.php": """<?php
#[Route('/blog', name: 'blog_')]
final class BlogController extends AbstractController
{
    #[Route('/', name: 'index')]
    public function index(): Response {}

    #[Route('/{slug}', name: 'post', methods: ['GET', 'POST'])]
    public function post(string $slug): Response {}
}
"""})
    assert _keys(app) == ["GET /blog", "GET /blog/{slug}", "POST /blog/{slug}"]


def test_a_plain_php_site_is_its_page_files(tmp_path):
    app = _app(tmp_path, {
        "public/index.php": "<?php include '../includes/head.php';", "public/contact.php": "<?php echo $_POST['m'];",
        "public/admin/index.php": "", "public/_partial.php": "", "public/includes/nav.php": "",
        "includes/head.php": "", "vendor/x/y.php": ""})
    assert _keys(app) == ["GET /", "GET /admin", "GET /contact.php", "POST /contact.php"]


def test_slim_and_express_and_python_and_rust(tmp_path):
    _app(tmp_path / "slim", {"index.php": "<?php $app->get('/hello/{name}', fn () => 1);\n"
                                          "$app->map(['GET', 'POST'], '/form', fn () => 1);"})
    _app(tmp_path / "express", {"server.js": "app.get('/users/:id', show);\nrouter.post(\"/users\", make);\n"
                                             "axios.get('/not/a/route');\n",
                                "tests/app.test.js": "app.get('/from/a/test', x);"})
    _app(tmp_path / "py", {"app.py": "@app.route('/a', methods=['GET', 'POST'])\ndef a(): ...\n"
                                     "@app.get('/items/{item_id}')\ndef b(): ...\n",
                           "shop/urls.py": "urlpatterns = [path('', views.home), path('p/<int:pk>/', views.p),\n"
                                           "    re_path(r'^y/(?P<year>[0-9]{4})/$', views.y),\n"
                                           "    path('api/', include('api.urls'))]\n"})
    _app(tmp_path / "rs", {"src/main.rs": 'let app = Router::new()\n    .route("/users/:id", get(show).delete(drop))\n'
                                          '    .route("/", get(home));\n#[post("/echo")]\nasync fn echo() {}\n'})
    assert _keys(tmp_path / "slim") == ["GET /form", "POST /form", "GET /hello/{name}"]
    assert _keys(tmp_path / "express") == ["POST /users", "GET /users/{id}"]
    assert _keys(tmp_path / "py") == ["GET /", "GET /a", "POST /a", "GET /items/{item_id}", "GET /p/{pk}",
                                      "GET /y/{year}"]
    assert _keys(tmp_path / "rs") == ["GET /", "POST /echo", "GET /users/{id}", "DELETE /users/{id}"]


def test_framework_route_lists_are_read():
    artisan = json.loads('[{"method": "GET|HEAD", "uri": "/"}, {"method": "POST", "uri": "posts"},'
                         '{"method": "PUT|PATCH", "uri": "posts/{post}"}]')
    assert [r.key for r in routes.from_artisan(artisan)] == ["GET /", "POST /posts", "PUT /posts/{post}",
                                                              "PATCH /posts/{post}"]
    symfony = {"blog_post": {"path": "/blog/{slug}", "method": "GET|POST"}, "home": {"path": "/", "method": "ANY"},
               "_wdt": {"path": "/_wdt/{token}", "method": "ANY"}}
    assert [r.key for r in routes.from_symfony(symfony)] == ["GET /blog/{slug}", "POST /blog/{slug}", "GET /"]


def test_requests_are_matched_to_their_route():
    keys = ["GET /entries/{id}", "GET /entries/new", "POST /entries/{id}/delete", "GET /"]
    assert routes.match(keys, "GET", "/entries/ab12cd34") == "GET /entries/{id}"
    assert routes.match(keys, "GET", "/entries/new") == "GET /entries/new"  # the literal route wins
    assert routes.match(keys, "GET", "/?page=2") == "GET /"
    assert routes.match(keys, "POST", "/entries/x/delete") == "POST /entries/{id}/delete"
    assert routes.match(keys, "GET", "/entries/a/b") is None and routes.match(keys, "DELETE", "/") is None
