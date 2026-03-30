# Sirco apt repo

bash
'''
sudo tee /etc/apt/sources.list.d/sirco.list >/dev/null <<'EOF'
deb [trusted=yes] https://sirco-dev.github.io/sircron-setup/ stable main
EOF
sudo apt-get update
sudo apt-get install -y sirco-full
sudo apt-get install -y sirco-all-desktop
'''
