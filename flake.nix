{
  description = "superpi: local small-model agent swarm";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      llama-cpp-cuda = pkgs.llama-cpp.override { cudaSupport = true; };
    in
    {
      devShells.${system}.default = pkgs.mkShell {
        packages = [
          llama-cpp-cuda
          pkgs.uv
          pkgs.sqlite3
          pkgs.jaq
        ];
        shellHook = ''
          echo "superpi devshell ready (llama.cpp with CUDA)"
        '';
      };
    };
}
