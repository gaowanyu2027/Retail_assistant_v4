package config

import "testing"

func TestLoadDefaults(t *testing.T) {
	t.Setenv("EDGE_LISTEN", "")
	t.Setenv("EDGE_BACKEND_URL", "")
	t.Setenv("EDGE_DIST_DIR", "")
	c := Load()
	if c.Listen != ":8000" || c.BackendURL != "http://127.0.0.1:8000" || c.DistDir != "frontend-vue/dist" {
		t.Fatalf("默认值不符: %+v", c)
	}
}

func TestLoadOverrides(t *testing.T) {
	t.Setenv("EDGE_LISTEN", ":8010")
	t.Setenv("EDGE_BACKEND_URL", "http://backend:8000/") // 尾斜杠应被去掉
	t.Setenv("EDGE_DIST_DIR", "/srv/dist")
	c := Load()
	if c.Listen != ":8010" || c.BackendURL != "http://backend:8000" || c.DistDir != "/srv/dist" {
		t.Fatalf("覆盖值不符: %+v", c)
	}
}
