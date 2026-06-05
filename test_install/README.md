# Test Install

To debug or reproduce installation issues, set up birdnet on this minimal docker container.

From this directory, run:

```.sh
docker build -t birdnet-test .
docker run -d --privileged --name birdnet-install birdnet-test
sleep 2  # let systemd initialize
docker exec -it birdnet-install su -l testbirder
```

You will then be in the container, with username `testbirder`.

The BirdNET-Pi [installation guide](https://github.com/mcguirepr89/BirdNET-Pi/wiki/Installation-Guide) assumes that you're using a Raspberry Pi imager, and then ssh-ing into it. We can skip that step, as setting up the docker container effectively does the same thing.

Then, following the installation guide, we'll run the following command inside the container:

```.sh
curl -s https://raw.githubusercontent.com/Nachtzuster/BirdNET-Pi/main/newinstaller.sh | bash
```

This will take a few minutes. If it completes successfully, the container will "reboot" - it will actually just shut down, so run the following commands to restart it:

```.sh
docker start birdnet-install
docker exec -it birdnet-install su -l testbirder
```

Then you should have a semi-functional birdnet installation. It won't have access to any hardware, but it should otherwise run just fine.

When you're done, delete the container and image:

```.sh
docker ps -a  # find the container name to use below
docker stop <CONTAINER_NAME> && docker rm <CONTAINER_NAME>
docker rmi birdnet-test
```