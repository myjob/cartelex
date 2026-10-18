
https://github.com/kovamd/kova#linux-package-managers
https://wiki.kova.md/installation/




## install hack

sudo sed -i '0,/Enabled: no/{s/Enabled: no/Enabled: yes/}' /etc/apt/sources.list.d/ubuntu.sources
sudo apt-get update
sudo apt-get install -y \
  libwebkit2gtk-4.1-dev \
  libayatana-appindicator3-dev \
  librsvg2-dev \
  patchelf
