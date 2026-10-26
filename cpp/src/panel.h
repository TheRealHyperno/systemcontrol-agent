// panel.h — the web control panel app (port of panel.rs)
#pragma once
#include <string>

namespace sysc {
// serve the control panel on 127.0.0.1:<port> (blocks forever)
void panel_serve(int port);
}  // namespace sysc
